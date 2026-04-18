"""
Transfer Engine - Scraping and invitation logic using Telethon.
Ported from the desktop app (main.py v24.0) to work as a headless service.
"""
import asyncio
import os
import time
import random
import logging

from telethon import TelegramClient
from telethon.tl.functions.channels import (
    GetParticipantsRequest, JoinChannelRequest, InviteToChannelRequest,
    GetFullChannelRequest,
)
from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
from telethon.tl.types import (
    ChannelParticipantsSearch, InputPeerUser, InputPhoneContact,
    Channel, User,
)
from telethon.errors import (
    FloodWaitError, UserPrivacyRestrictedError, UserNotMutualContactError,
    ChatWriteForbiddenError, PeerFloodError, UserBannedInChannelError,
    SessionPasswordNeededError,
)

import database as db

logger = logging.getLogger("engine")

API_ID = int(os.environ.get("API_ID", "38850365"))
API_HASH = os.environ.get("API_HASH", "9d5791e389f69ed261ee3e40b4b8ddd1")
MAX_PER_ACCOUNT = 40  # 40 members per phone number


# ═══════════════════════════════════════════
# Session Management
# ═══════════════════════════════════════════
def get_client(phone):
    """Create a TelegramClient for the given phone number."""
    session_path = os.path.join(db.SESSIONS_DIR, phone)
    return TelegramClient(session_path, API_ID, API_HASH)


async def register_phone(phone, on_code_needed, on_2fa_needed=None):
    """
    Register a phone number. Returns the connected client.
    on_code_needed: async callback that returns the verification code.
    on_2fa_needed: async callback that returns the 2FA password.
    """
    client = get_client(phone)
    await client.connect()

    if await client.is_user_authorized():
        return client, "already_authorized"

    await client.send_code_request(phone)
    code = await on_code_needed()

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        if on_2fa_needed:
            password = await on_2fa_needed()
            await client.sign_in(password=password)
        else:
            await client.disconnect()
            return None, "2fa_required"

    return client, "success"


# ═══════════════════════════════════════════
# Scraping Engine
# ═══════════════════════════════════════════
async def scrape_members(phone, source_group, progress_callback=None):
    """
    Scrape members from a group/channel.
    Returns list of member dicts.
    """
    client = get_client(phone)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        return None, "غير مسجل الدخول"

    try:
        entity = await client.get_entity(source_group)
    except Exception as e:
        await client.disconnect()
        return None, f"ما لقيت القروب: {e}"

    title = getattr(entity, 'title', source_group)
    if progress_callback:
        await progress_callback(f"🔍 جاري سحب الأعضاء من: {title}")

    all_members = []
    seen_ids = set()
    offset = 0
    batch = 200

    while True:
        try:
            participants = await client(GetParticipantsRequest(
                entity, ChannelParticipantsSearch(''), offset, batch, hash=0
            ))

            if not participants.users:
                break

            for user in participants.users:
                if user.bot or user.deleted:
                    continue
                if user.id in seen_ids:
                    continue
                seen_ids.add(user.id)

                all_members.append({
                    "id": user.id,
                    "access_hash": str(user.access_hash) if user.access_hash else "0",
                    "username": user.username or "",
                    "first_name": user.first_name or "",
                    "last_name": user.last_name or "",
                })

            offset += len(participants.users)

            if progress_callback:
                await progress_callback(f"📥 تم سحب {len(all_members)} عضو...")

            if len(participants.users) < batch:
                break

            await asyncio.sleep(1.5)

        except FloodWaitError as e:
            if progress_callback:
                await progress_callback(f"⏳ فلود! انتظار {e.seconds} ثانية...")
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            if progress_callback:
                await progress_callback(f"⚠️ خطأ: {str(e)[:50]}")
            break

    await client.disconnect()

    # Save to database
    db.save_members(all_members)

    return all_members, f"✅ تم سحب {len(all_members)} عضو من {title}"


async def scrape_active_members(phone, source_group, message_limit=5000, progress_callback=None):
    """
    Scrape members from a group's chat history by collecting users who sent messages.
    Useful for groups with hidden members list.
    """
    client = get_client(phone)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        return None, "غير مسجل الدخول"

    try:
        entity = await client.get_entity(source_group)
    except Exception as e:
        await client.disconnect()
        return None, f"ما لقيت القروب: {e}"

    title = getattr(entity, 'title', source_group)
    if progress_callback:
        await progress_callback(f"🔍 جاري سحب المتفاعلين من دردشة: {title}")

    all_members = {}  # Using dict to deduplicate by user_id
    count = 0

    try:
        # iter_messages is the standard telethon way to fetch history
        async for message in client.iter_messages(entity, limit=message_limit):
            user = message.sender
            if user and isinstance(user, User) and not user.bot and not user.deleted:
                if user.id not in all_members:
                    all_members[user.id] = {
                        "id": user.id,
                        "access_hash": str(user.access_hash) if user.access_hash else "0",
                        "username": user.username or "",
                        "first_name": user.first_name or "",
                        "last_name": user.last_name or "",
                    }
            
            count += 1
            if progress_callback and count % 500 == 0:
                await progress_callback(f"📥 فحصنا {count} رسالة... لقينا {len(all_members)} عضو متفاعل.")
                await asyncio.sleep(1) # prevent flood

    except FloodWaitError as e:
        if progress_callback:
            await progress_callback(f"⏳ فلود! انتظار {e.seconds} ثانية...")
        await asyncio.sleep(e.seconds + 2)
    except Exception as e:
        if progress_callback:
             await progress_callback(f"⚠️ خطأ أثناء السحب: {str(e)[:50]}")

    await client.disconnect()

    result_list = list(all_members.values())
    
    # Save to database
    db.save_members(result_list)

    return result_list, f"✅ تم سحب {len(result_list)} عضو متفاعل من {title}"



# ═══════════════════════════════════════════
# Invitation Engine
# ═══════════════════════════════════════════
async def invite_members(target_group, progress_callback=None, stop_check=None):
    """
    Invite scraped members to target group.
    Uses all registered accounts with rotation.
    40 members per account.
    """
    members = db.get_members()
    if not members:
        if progress_callback:
            await progress_callback("❌ لا يوجد أعضاء محفوظين! اسحب أولاً.")
        return 0, 0

    # Filter already added
    added_set = db.get_added_users()
    filtered = []
    for m in members:
        uid = str(m.get("id", ""))
        uname = m.get("username", "")
        if uid not in added_set and uname not in added_set:
            filtered.append(m)

    if not filtered:
        if progress_callback:
            await progress_callback("✅ جميع الأعضاء تمت إضافتهم مسبقاً!")
        return 0, 0

    members = filtered
    total = len(members)

    # Get available accounts
    phones = db.get_accounts()
    # Filter out flooded accounts
    phones = [p for p in phones if not db.is_flooded(p)]

    if not phones:
        if progress_callback:
            await progress_callback("❌ لا يوجد حسابات جاهزة! كلها محظورة أو غير مسجلة.")
        return 0, 0

    if progress_callback:
        await progress_callback(
            f"🚀 بدء الإضافة\n"
            f"👥 الأعضاء: {total}\n"
            f"📱 الحسابات: {len(phones)}\n"
            f"📊 الحد: {MAX_PER_ACCOUNT} لكل رقم"
        )

    added = 0
    failed = 0
    i = 0

    for phone_idx, phone in enumerate(phones):
        if i >= total:
            break

        if stop_check and stop_check():
            break

        client = get_client(phone)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                if progress_callback:
                    await progress_callback(f"⚠️ {phone} غير مسجل.. تخطي")
                continue

            # Join target group
            try:
                await client(JoinChannelRequest(target_group))
            except Exception:
                pass

            # Get target entity
            try:
                target_entity = await asyncio.wait_for(
                    client.get_entity(target_group), timeout=15.0
                )
            except Exception as e:
                if progress_callback:
                    await progress_callback(f"❌ {phone}: ما لقى القروب - {str(e)[:30]}")
                await client.disconnect()
                continue

            is_channel = getattr(target_entity, 'broadcast', False)
            title = getattr(target_entity, 'title', target_group)

            if progress_callback:
                kind = "القناة" if is_channel else "المجموعة"
                await progress_callback(
                    f"📱 الحساب: {phone} ({phone_idx+1}/{len(phones)})\n"
                    f"🎯 {kind}: {title}"
                )

            # Warm-up
            try:
                await client.get_messages(target_entity, limit=3)
                await asyncio.sleep(random.uniform(2, 5))
            except Exception:
                pass

            account_added = 0
            consecutive_privacy = 0

            while i < total and account_added < MAX_PER_ACCOUNT:
                if stop_check and stop_check():
                    break

                member = members[i]
                username = member.get("username", "")
                user_id = member.get("id")
                access_hash = member.get("access_hash", "0")
                display = f"@{username}" if username else f"ID:{user_id}"

                # Resolve user entity
                user_entity = None
                if username and username != "None":
                    try:
                        user_entity = await asyncio.wait_for(
                            client.get_entity(username), timeout=5.0
                        )
                    except Exception:
                        pass

                if not user_entity and user_id and str(user_id).isdigit():
                    try:
                        uhash = int(access_hash or 0)
                        if uhash != 0:
                            user_entity = InputPeerUser(int(user_id), uhash)
                        else:
                            try:
                                user_entity = await asyncio.wait_for(
                                    client.get_entity(int(user_id)), timeout=5.0
                                )
                            except Exception:
                                pass
                    except (ValueError, TypeError):
                        pass

                if not user_entity:
                    failed += 1
                    i += 1
                    continue

                # Contact Injection
                try:
                    await client(ImportContactsRequest([
                        InputPhoneContact(
                            client_id=random.randint(0, 999999),
                            phone="", first_name=display, last_name=""
                        )
                    ]))
                except Exception:
                    pass

                # Attempt invitation
                try:
                    if is_channel:
                        await asyncio.wait_for(
                            client(InviteToChannelRequest(target_entity, [user_entity])),
                            timeout=30.0
                        )
                    else:
                        await asyncio.wait_for(
                            client(InviteToChannelRequest(target_entity, [user_entity])),
                            timeout=30.0
                        )

                    added += 1
                    account_added += 1
                    consecutive_privacy = 0

                    uid = str(getattr(user_entity, 'user_id', getattr(user_entity, 'id', user_id)))
                    db.mark_added(uid)

                    if progress_callback and added % 2 == 0:
                        await progress_callback(
                            f"📱 {phone} | ✅ {display}\n"
                            f"📊 نجح: {added} | فشل: {failed} | باقي: {total-i-1}\n"
                            f"📈 هذا الحساب: {account_added}/{MAX_PER_ACCOUNT}"
                        )

                    i += 1

                    # Smart delay
                    if account_added < MAX_PER_ACCOUNT and i < total:
                        delay = random.uniform(35, 75)
                        if progress_callback and account_added % 5 == 0:
                            await progress_callback(f"⏳ انتظار {delay:.0f} ثانية...")
                        await asyncio.sleep(delay)

                except (UserPrivacyRestrictedError, UserNotMutualContactError):
                    failed += 1
                    consecutive_privacy += 1
                    uid = str(user_id or "")
                    if uid:
                        db.mark_added(uid)
                    i += 1

                    if consecutive_privacy >= 10:
                        if progress_callback:
                            await progress_callback(
                                f"⚠️ {phone}: 10 خصوصية متتالية.. تدوير"
                            )
                        break

                except FloodWaitError as e:
                    db.set_flood(phone, e.seconds)
                    if progress_callback:
                        h = e.seconds // 3600
                        m = (e.seconds % 3600) // 60
                        await progress_callback(
                            f"🚫 {phone}: فلود {h}س {m}د.. تدوير للحساب التالي"
                        )
                    break

                except (PeerFloodError, ChatWriteForbiddenError, UserBannedInChannelError):
                    if progress_callback:
                        await progress_callback(f"🚫 {phone}: مقيّد.. تدوير")
                    break

                except Exception as e:
                    failed += 1
                    i += 1
                    err = str(e)[:40]
                    if "AuthKey" in type(e).__name__ or "Unauthorized" in type(e).__name__:
                        if progress_callback:
                            await progress_callback(f"🔒 {phone}: جلسة منتهية.. تدوير")
                        break

            if progress_callback:
                await progress_callback(
                    f"✅ {phone} انتهى: أضاف {account_added} عضو"
                )

            await client.disconnect()

        except Exception as e:
            if progress_callback:
                await progress_callback(f"❌ خطأ {phone}: {str(e)[:40]}")
            try:
                await client.disconnect()
            except Exception:
                pass

    # Final report
    if progress_callback:
        await progress_callback(
            f"🏁 انتهت العملية!\n\n"
            f"✅ نجح: {added}\n"
            f"❌ فشل: {failed}\n"
            f"📱 حسابات مستخدمة: {min(phone_idx+1, len(phones))}"
        )

    return added, failed
