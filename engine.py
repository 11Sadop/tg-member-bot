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

async def _invite_worker(phone, phone_idx, target_group, members_chunk, shared_state, stop_check, progress_queue):
    """Worker task for a single phone number"""
    if not members_chunk:
        return 0, 0
    
    added = 0
    failed = 0
    client = get_client(phone)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            progress_queue.append(f"⚠️ {phone} غير مسجل.. تخطي")
            return 0, len(members_chunk)

        try:
            await client(JoinChannelRequest(target_group))
        except Exception:
            pass

        try:
            target_entity = await asyncio.wait_for(client.get_entity(target_group), timeout=15.0)
        except Exception as e:
            progress_queue.append(f"❌ {phone}: ما لقى القروب - {str(e)[:30]}")
            await client.disconnect()
            return 0, len(members_chunk)

        is_channel = getattr(target_entity, 'broadcast', False)
        
        try:
            await client.get_messages(target_entity, limit=3)
            await asyncio.sleep(random.uniform(2, 5))
        except Exception:
            pass

        consecutive_privacy = 0

        for member in members_chunk:
            if stop_check and stop_check():
                break

            username = member.get("username", "")
            user_id = member.get("id")
            access_hash = member.get("access_hash", "0")
            display = f"@{username}" if username else f"ID:{user_id}"

            # Resolve user entity
            user_entity = None
            if username and username != "None":
                try:
                    user_entity = await asyncio.wait_for(client.get_entity(username), timeout=5.0)
                except Exception:
                    pass

            if not user_entity and user_id and str(user_id).isdigit():
                try:
                    uhash = int(access_hash or 0)
                    if uhash != 0:
                        user_entity = InputPeerUser(int(user_id), uhash)
                    else:
                        try:
                            user_entity = await asyncio.wait_for(client.get_entity(int(user_id)), timeout=5.0)
                        except Exception:
                            pass
                except (ValueError, TypeError):
                    pass

            if not user_entity:
                failed += 1
                shared_state['failed'] += 1
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
                await asyncio.wait_for(
                    client(InviteToChannelRequest(target_entity, [user_entity])),
                    timeout=30.0
                )

                added += 1
                shared_state['added'] += 1
                consecutive_privacy = 0

                uid = str(getattr(user_entity, 'user_id', getattr(user_entity, 'id', user_id)))
                db.mark_added(uid)

                # Smart delay (Reduced for speed!)
                if added < MAX_PER_ACCOUNT:
                    delay = random.uniform(15, 30)
                    await asyncio.sleep(delay)

            except (UserPrivacyRestrictedError, UserNotMutualContactError):
                failed += 1
                shared_state['failed'] += 1
                consecutive_privacy += 1
                uid = str(user_id or "")
                if uid:
                    db.mark_added(uid)

                if consecutive_privacy >= 10:
                    break

            except FloodWaitError as e:
                db.set_flood(phone, e.seconds)
                break

            except (PeerFloodError, ChatWriteForbiddenError, UserBannedInChannelError):
                break

            except Exception as e:
                failed += 1
                shared_state['failed'] += 1
                if "AuthKey" in type(e).__name__ or "Unauthorized" in type(e).__name__:
                    break

        await client.disconnect()

    except Exception:
        try:
            await client.disconnect()
        except:
            pass

    return added, failed


async def invite_members(target_group, progress_callback=None, stop_check=None):
    """
    Invite scraped members to target group concurrently.
    Uses all registered accounts AT THE SAME TIME.
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

    # Get available accounts
    phones = db.get_accounts()
    phones = [p for p in phones if not db.is_flooded(p)]

    if not phones:
        if progress_callback:
            await progress_callback("❌ لا يوجد حسابات جاهزة! كلها محظورة أو غير مسجلة.")
        return 0, 0

    # Distribute members across available phones (MAX_PER_ACCOUNT per phone)
    tasks = []
    shared_state = {'added': 0, 'failed': 0}
    progress_queue = []
    
    # Slice the members
    members_to_take = min(len(members), len(phones) * MAX_PER_ACCOUNT)
    members = members[:members_to_take]
    total_assigned = len(members)

    chunk_idx = 0
    for phone_idx, phone in enumerate(phones):
        if chunk_idx >= len(members):
            break
        # Take up to MAX_PER_ACCOUNT
        chunk = members[chunk_idx : chunk_idx + MAX_PER_ACCOUNT]
        chunk_idx += len(chunk)
        
        # Start worker task
        tasks.append(
            asyncio.create_task(_invite_worker(phone, phone_idx, target_group, chunk, shared_state, stop_check, progress_queue))
        )

    active_phones_count = len(tasks)

    if progress_callback:
        await progress_callback(
            f"🚀 بدء الإضافة (بشكل متزامن ⚡)\n"
            f"👥 الأعضاء المخصصين: {total_assigned}\n"
            f"📱 الحسابات الفعالة: {active_phones_count}\n"
            f"⏳ جارِ تشغيل جميع الحسابات في نفس الوقت..."
        )

    # Reporter loop runs while tasks are active
    async def reporter():
        last_added = -1
        last_failed = -1
        while not all(t.done() for t in tasks):
            if stop_check and stop_check():
                for t in tasks:
                    if not t.done():
                        t.cancel()
                break
                
            cur_added = shared_state['added']
            cur_failed = shared_state['failed']
            
            # Update only if values changed
            if cur_added != last_added or cur_failed != last_failed or progress_queue:
                msg = f"⚡ جاري الإضافة المتزامنة ({active_phones_count} حساب)...\n\n"
                msg += f"✅ نجح: {cur_added}\n"
                msg += f"❌ فشل: {cur_failed}\n"
                msg += f"📊 باقي بالدفعة: {total_assigned - (cur_added + cur_failed)}"
                
                # consume some alerts if present
                if progress_queue:
                    alerts = progress_queue[-2:] # show last two
                    progress_queue.clear()
                    msg += "\n\n🔔 آخر التنبيهات:\n" + "\n".join(alerts)
                
                if progress_callback:
                    try:
                        await progress_callback(msg)
                    except: pass
                
                last_added = cur_added
                last_failed = cur_failed
            
            await asyncio.sleep(4)

    reporter_task = asyncio.create_task(reporter())
    
    # Wait for all workers to finish
    await asyncio.gather(*tasks, return_exceptions=True)
    await reporter_task

    total_added = shared_state['added']
    total_failed = shared_state['failed']

    # Final report
    if progress_callback:
        await progress_callback(
            f"🏁 انتهت العملية المتزامنة!\n\n"
            f"✅ المجموع الناجح: {total_added}\n"
            f"❌ المجموع الفاشل: {total_failed}\n"
            f"📱 الحسابات التي اشتغلت: {active_phones_count}"
        )

    return total_added, total_failed
