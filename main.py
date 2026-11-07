# -*- coding: utf-8 -*-
"""XApis CLI —— 登录 / 发作品 / 看详情 / 搜索 / 维护注册表。

    python main.py whoami
    python main.py detail  <推文id或链接>
    python main.py user    <用户名>
    python main.py search  "关键词" [--product Latest]
    python main.py post    "正文" [--image a.jpg --image b.png] [--reply-to <id>]
    python main.py post    --file long.txt          # 超过 280 权重自动走长推
    python main.py thread  --file thread.md         # 单独一行 --- 分隔每条
    python main.py chat-send <用户名> "正文" --keys keys.json
    python main.py article article.md [--cover banner.png] [--publish]
    python main.py article-list [--published]
    python main.py article-delete <文章id>
    python main.py delete  <推文id>
    python main.py spider  <用户名> [--pages 3] [--save all]
    python main.py refresh-graphql
    python main.py refresh-castle [--check]
    python main.py login

对齐 ../DouYin_Spider/main.py 的定位：既是入口，也是每条链路的最小可运行示例。
"""

import argparse
import json
import os
import re
import sys

from loguru import logger

from utils.common_util import clear_cookies, init, save_cookies
from utils.data_util import handle_work_info, download_work, save_to_xlsx
from utils.x_util import (TWEET_WEIGHT_LIMIT, extract_cursor, extract_tweet_entries,
                          parse_screen_name, trans_cookies, tweet_weight)
from x_apis.errors import GraphQLError
from x_apis.x_api import NOTIFICATION_TYPES, XAPI
from x_apis.x_article_api import REPLY_MODES, XArticleAPI
from x_apis.x_write_api import XWriteAPI


def _dump(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_whoami(auth, args):
    if not auth.is_logged_in:
        logger.error('.env 里的 X_COOKIES 没有 auth_token，会话未登录')
        return 1
    result = XAPI.get_viewer(auth)['data']['viewer']['user_results']['result']
    logger.info(f"@{result['core']['screen_name']} ({result['rest_id']}) "
                f"{result['core']['name']}")
    return 0


def cmd_detail(auth, args):
    res_json = XAPI.get_work_info(auth, args.work)
    entries = extract_tweet_entries(res_json)
    if not entries:
        logger.error('没有解析到推文，可能是私密账号或已删除')
        return 1
    _dump(handle_work_info(entries[0]))
    logger.info(f'同页评论 {len(entries) - 1} 条')
    return 0


def cmd_user(auth, args):
    res_json = XAPI.get_user_info(auth, args.name)
    result = res_json['data']['user']['result']
    _dump({
        'rest_id': result['rest_id'],
        'name': result['core']['name'],
        'screen_name': result['core']['screen_name'],
        'followers': (result.get('relationship_counts') or {}).get('followers'),
        'tweets': (result.get('tweet_counts') or {}).get('tweets'),
        'description': (result.get('profile_bio') or {}).get('description'),
    })
    return 0


def cmd_search(auth, args):
    res_json = XAPI.search_work(auth, args.query, product=args.product)
    if args.product == 'Lists':
        from x_apis.response_validation import validated_list_search_page
        _dump(validated_list_search_page(res_json))
        return 0
    works = [handle_work_info(item) for item in extract_tweet_entries(res_json)]
    for work in works:
        logger.info(f"{work['work_url']}  {work['title'][:60]}")
    logger.info(f'共 {len(works)} 条')
    return 0


def cmd_notifications(auth, args):
    from utils.notification_util import parse_notification_page
    from x_apis.client_errors import XClientError
    try:
        raw = XAPI.get_notifications(auth, args.type, count=args.count, cursor=args.cursor)
        page = parse_notification_page(raw)
        _dump({'type': args.type, **page})
        return 0
    except GraphQLError as exc:
        _dump({'type': args.type, 'status': 'error',
               'error_codes': [error.get('code') for error in exc.errors],
               'error_names': [error.get('name') for error in exc.errors]})
        return 1
    except XClientError as exc:
        _dump({'type': args.type, 'status': 'unavailable' if exc.code == 'capability_unavailable' else 'error',
               'code': exc.code, 'message': str(exc), 'details': exc.result})
        return 1
    except Exception as exc:
        logger.error('通知读取失败：{}', type(exc).__name__)
        return 1


def cmd_watch_users(auth, args):
    import signal
    import threading
    from dotenv import dotenv_values
    from pathlib import Path
    from x_apis import XClient, XClientError, UserPostMonitor, Webhook

    stop = threading.Event()
    previous = {}
    try:
        if args.status_only and args.retry_failed:
            raise ValueError('查看状态时不能重放事件')
        if args.duration is not None and args.duration <= 0:
            raise ValueError('运行时长必须为正数')
        config = {**dotenv_values(args.env), **os.environ}
        expected_posts = None
        if args.reference_posts:
            if args.metrics_file and Path(args.reference_posts).resolve() == Path(args.metrics_file).resolve():
                raise ValueError('对照清单与指标输出不能使用同一文件')
            with open(args.reference_posts, encoding='utf-8') as source:
                expected_posts = json.load(source)
        if args.webhook:
            handler = Webhook(config.get('X_MONITOR_WEBHOOK_URL') or '',
                              config.get('X_MONITOR_WEBHOOK_SECRET') or '')
        else:
            def handler(event):
                print(json.dumps(event, ensure_ascii=False), flush=True)

        def report(state):
            if expected_posts is not None:
                state = {**state, 'comparison': monitor.compare_posts(expected_posts)}
            print(json.dumps({'type': 'monitor.status', **state}, ensure_ascii=False),
                  file=sys.stderr, flush=True)
            if args.metrics_file:
                import tempfile
                destination = Path(args.metrics_file)
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent,
                                                     prefix='.monitor-', delete=False) as output:
                        temporary = output.name
                        json.dump(state, output, ensure_ascii=False, indent=2)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, destination)
                finally:
                    if temporary and os.path.exists(temporary):
                        os.unlink(temporary)

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
        options = {'interval': args.interval, 'max_pages': args.max_pages,
                   'concurrency': args.concurrency, 'max_rps': args.max_rps,
                   'scan_page_limit': args.scan_page_limit, 'rescan_interval': args.rescan_interval}
        if args.guest:
            state_dir = Path(args.state_dir or config.get('X_CHAT_STATE_DIR') or '.xapis')
            if not state_dir.is_absolute():
                state_dir = Path(args.env).resolve().parent / state_dir
            monitor = UserPostMonitor.guest(args.names, proxy=config.get('X_PROXY') or None,
                                            state_dir=state_dir, **options)
        else:
            client = XClient.from_env(args.env, **({'state_dir': args.state_dir} if args.state_dir else {}))
            monitor = client.users.monitor(args.names, **options)
        with monitor:
            if args.retry_failed:
                monitor.retry_failed()
            if args.status_only:
                report(monitor.status())
                return 0
            if args.once:
                monitor.poll_once(stop=stop)
                state = monitor.deliver_once(handler, stop=stop)
                report(state)
                return int(any(t['last_error'] or t.get('backfill_error') for t in state['targets']) or
                           state['delivery']['failed'] > 0 or state['delivery']['pending'] > 0)
            try:
                monitor.run(handler, stop=stop, on_status=report, duration=args.duration)
            finally:
                report(monitor.status())
        return 0
    except XClientError as exc:
        print(json.dumps({'type': 'monitor.error', 'code': exc.code}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (ValueError, TypeError):
        print('监听参数或 Webhook 配置无效，请检查 --help 和本地配置', file=sys.stderr)
        return 2
    except Exception as exc:
        # 网络异常可能包含代理凭证，仅输出异常类型，不打印原始内容。
        print(json.dumps({'type': 'monitor.error', 'code': 'monitor_failed',
                          'error_type': type(exc).__name__}), file=sys.stderr)
        return 1
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def _chat_pin(args, *, confirm=False):
    import getpass
    import stat
    from utils.xchat_identity import IdentityError
    if args.pin_file:
        fd = os.open(args.pin_file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, encoding='utf-8') as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024:
                raise IdentityError('PIN 文件必须为权限 0600 的小型普通文件')
            pin = f.read().rstrip('\r\n')
    else:
        if not sys.stdin.isatty():
            raise IdentityError('请在终端隐藏输入 PIN，或使用 --pin-file；不要把 PIN 放在参数中')
        pin = getpass.getpass('X Chat PIN: ')
        if confirm and pin != getpass.getpass('再次输入 X Chat PIN: '):
            raise IdentityError('两次 PIN 不一致')
    if not pin:
        raise IdentityError('PIN 不能为空')
    return pin


def cmd_chat_identity(auth, args):
    from utils.xchat_identity import IdentityError, inspect_identity, setup_identity, unlock_identity
    try:
        current = inspect_identity(auth, args.state if args.command != 'chat-unlock' else None)
        if args.command == 'chat-status' or not args.apply:
            _dump({'operation': args.command, **current, 'applied': False})
            return 0
        if args.command == 'chat-setup':
            if current['state'] != 'needs_setup' and not args.resume:
                raise IdentityError('账号已有公钥，请使用 chat-unlock；不会重置身份')
            pin = _chat_pin(args, confirm=True)
            result = setup_identity(auth, pin, args.state, resume=args.resume)
        else:
            if current['state'] == 'needs_setup':
                raise IdentityError('账号尚未初始化，请使用 chat-setup')
            if os.path.lexists(args.state):
                raise IdentityError('目标身份文件已存在，请指定新文件；不会覆盖现有私钥')
            pin = _chat_pin(args)
            result = unlock_identity(auth, pin, args.state, version=args.key_version)
        _dump(result)
        return 0
    except IdentityError as exc:
        logger.error('{}', exc)  # IdentityError messages never contain secrets.
        return 1
    except Exception as exc:
        logger.error('X Chat 身份操作失败：{}；未自动重试', type(exc).__name__)
        return 1


def cmd_chat_session(auth, args):
    from utils.xchat_identity import IdentityError, export_conversation_material
    try:
        result = export_conversation_material(auth, args.state, args.conversation, args.out,
            signature_version=args.signature_version,
            contents_encryption_version=args.contents_version or None)
        _dump(result)
        return 0
    except IdentityError as exc:
        logger.error('{}', exc)
        return 1
    except Exception as exc:
        logger.error('X Chat 会话密钥准备失败：{}', type(exc).__name__)
        return 1


def _read_text(text, file):
    if file:
        with open(file, encoding='utf-8') as fp:
            return fp.read().strip()
    return text


def cmd_message(auth, args):
    from x_apis import XClient, XClientError
    client = XClient.from_env()
    try:
        if args.command == 'message-status':
            result = client.messages.reconcile(args.request_id)
        elif args.command == 'messages':
            result = client.messages.list(args.username, limit=args.limit, before=args.before)
        elif not args.apply:
            result = client.messages.preview(args.username)
        else:
            result = client.messages.send(args.username, _read_text(args.text,args.file),
                                          request_id=args.request_id,mode=args.mode)
        _dump(result)
        return 0
    except XClientError as exc:
        _dump({'error':exc.code,'message':str(exc),'result':exc.result})
        return 1
    except Exception as exc:
        _dump({'error':type(exc).__name__})
        return 1


def cmd_chat_start(auth, args):
    from utils.xchat_conversation import start_conversation
    from utils.xchat_identity import IdentityError
    try:
        _dump(start_conversation(auth, args.username, args.state, args.out,
                                 args.receipt, apply=args.apply, resume=args.resume))
        return 0
    except IdentityError as exc:
        logger.error('新会话未完成：{}', str(exc))
        return 1
    except Exception as exc:
        logger.error('新会话未完成：{}；保留原回执，使用 --resume 回读核对，勿重建', type(exc).__name__)
        return 1


def cmd_chat_request(auth, args):
    from utils.xchat_conversation import send_first_request
    from utils.xchat_identity import IdentityError
    try:
        result = send_first_request(auth, args.username, _read_text(args.text, args.file),
                                    args.receipt, apply=args.apply, resume=args.resume)
        _dump(result)
        return 1 if result['stage'] == 'blocked' else 0
    except IdentityError as exc:
        logger.error('首次消息未完成：{}', str(exc))
        return 1
    except Exception as exc:
        logger.error('首次消息未完成：{}；保留回执，使用 --resume 核对，勿重发', type(exc).__name__)
        return 1


def _write_chat_receipt(fp, record):
    """Persist the attempt marker before the one allowed send call."""
    fp.seek(0)
    json.dump(record, fp, ensure_ascii=False, indent=2)
    fp.write('\n')
    fp.truncate()
    fp.flush()
    os.fsync(fp.fileno())


def cmd_chat_send(auth, args):
    """Prepare locally by default; --apply verifies both identities and sends once."""
    record, receipt_file, send_started = None, None, False
    try:
        from utils.xchat_codec import message_event_summary
        from utils.xchat_crypto import load_key_material, prepare_text_message
        from x_apis.x_dm_api import XChatAPI

        if args.apply and not args.receipt:
            logger.error('chat-send --apply 必须指定新的 --receipt 文件')
            return 1
        recipient = parse_screen_name(args.username)
        if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', recipient):
            logger.error('chat-send 用户名无效')
            return 1
        text = _read_text(args.text, args.file)
        if not text or not text.strip():
            logger.error('私信正文为空：给 text 参数或 --file')
            return 1
        keys = load_key_material(args.keys)
        if not re.fullmatch(r'[1-9][0-9]*', keys.sender_id):
            logger.error('密钥材料的 sender_id 必须为数字用户 ID')
            return 1
        prepared = prepare_text_message(text, keys)
        record = {
            'recipient': recipient,
            'sender_id': keys.sender_id,
            'conversation_id': prepared.conversation_id,
            'message_id': prepared.message_id,
            'stage': 'prepared',
        }
        if not args.apply:
            _dump(record)
            logger.info('仅完成本地准备，未发送；--apply 才会校验账号并发送')
            return 0

        # O_EXCL also rejects existing symlinks. Keep the same descriptor for
        # every update so a reused receipt can never authorize another POST.
        fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        receipt_file = os.fdopen(fd, 'w', encoding='utf-8')
        record['stage'] = 'checking_sender'
        _write_chat_receipt(receipt_file, record)
        viewer = XAPI.get_viewer(auth)['data']['viewer']['user_results']['result']
        if viewer.get('rest_id') != keys.sender_id:
            record['stage'] = 'blocked_sender_mismatch'
            _write_chat_receipt(receipt_file, record)
            logger.error('当前会话账号与密钥 sender_id 不一致，未发送')
            return 1

        record['stage'] = 'checking_recipient'
        _write_chat_receipt(receipt_file, record)
        user = XAPI.get_user_info(auth, recipient)['data']['user']['result']
        core = user.get('core')
        resolved_name = core.get('screen_name') if isinstance(core, dict) else None
        if not isinstance(resolved_name, str) or resolved_name.lower() != recipient:
            record['stage'] = 'blocked_handle_mismatch'
            _write_chat_receipt(receipt_file, record)
            logger.error('接口返回的用户名与指定收件人不一致，未发送')
            return 1
        recipient_id = user.get('rest_id')
        if (not isinstance(recipient_id, str)
                or not re.fullmatch(r'[1-9][0-9]*', recipient_id)):
            record['stage'] = 'blocked_invalid_recipient'
            _write_chat_receipt(receipt_file, record)
            logger.error('无法确认收件人的数字用户 ID，未发送')
            return 1
        record['recipient_id'] = recipient_id
        expected_conversation = ':'.join(sorted(
            (keys.sender_id, recipient_id), key=int))
        if expected_conversation != keys.conversation_id:
            record['stage'] = 'blocked_recipient_mismatch'
            _write_chat_receipt(receipt_file, record)
            logger.error('收件人 ID 与密钥会话不一致，未发送')
            return 1
        # Profile lookup resolves identity; X Chat's member query supplies
        # authoritative chat permissions for that exact resolved user.
        record['stage'] = 'checking_chat_permission'
        _write_chat_receipt(receipt_file, record)
        members_payload = XChatAPI.get_users_by_ids(auth, [recipient_id])
        if not isinstance(members_payload, dict) or members_payload.get('errors'):
            raise RuntimeError('X Chat member query failed')
        data = members_payload.get('data')
        members = data.get('get_member_results') if isinstance(data, dict) else None
        entries = members.get('results') if isinstance(members, dict) else None
        member = entries[0] if isinstance(entries, list) and len(entries) == 1 else None
        member_results = member.get('member_results') if isinstance(member, dict) else None
        chat_user = member_results.get('result') if isinstance(member_results, dict) else None
        chat_core = chat_user.get('core') if isinstance(chat_user, dict) else None
        chat_name = chat_core.get('screen_name') if isinstance(chat_core, dict) else None
        if (not isinstance(member, dict) or member.get('id') != recipient_id
                or member.get('status') != 'Found'
                or not isinstance(member_results, dict)
                or member_results.get('rest_id') != recipient_id
                or not isinstance(chat_user, dict)
                or chat_user.get('__typename') != 'User'
                or chat_user.get('rest_id') != recipient_id
                or not isinstance(chat_name, str) or chat_name.lower() != recipient):
            record['stage'] = 'blocked_chat_member_mismatch'
            _write_chat_receipt(receipt_file, record)
            logger.error('X Chat 成员结果无法唯一确认指定收件人，未发送')
            return 1
        permissions = chat_user.get('chat_permissions')
        if not isinstance(permissions, dict) or permissions.get('can_dm') is not True:
            record['stage'] = 'blocked_dm_permission'
            _write_chat_receipt(receipt_file, record)
            logger.error('收件人的 can_dm 未明确允许私信，未发送')
            return 1

        record['stage'] = 'sending'
        _write_chat_receipt(receipt_file, record)
        send_started = True
        payload = XChatAPI.send_message(auth, **prepared.as_send_kwargs())
        event = payload['data']['xchat_send_create_message_event']['encoded_message_event']
        record['receipt'] = message_event_summary(event)
        record['stage'] = 'confirmed'
        _write_chat_receipt(receipt_file, record)
        _dump(record)
        return 0
    except FileExistsError:
        logger.error('receipt 文件已存在；已阻止再次发送，请先核对原消息结果')
        return 1
    except Exception as exc:
        error = {'type': type(exc).__name__}
        response = getattr(exc, 'response', None)
        http_status = getattr(response, 'status_code', None)
        if type(http_status) is int:
            error['status_code'] = http_status
        if receipt_file is not None and record is not None:
            record['stage'] = 'unconfirmed' if send_started else 'failed_before_send'
            record['error'] = error
            try:
                _write_chat_receipt(receipt_file, record)
            except OSError:
                logger.error('receipt 更新失败；保留已有标记，勿直接重发')
        logger.error('X Chat 未完成：{}；发送结果以 receipt 为准，勿直接重发',
                     error['type'])
        return 1
    finally:
        if receipt_file is not None:
            receipt_file.close()


def cmd_post(auth, args):
    text = _read_text(args.text, args.file)
    if not text:
        logger.error('正文为空：给 text 参数或 --file')
        return 1
    note = True if args.note else None
    weight = tweet_weight(text)
    if note or weight > TWEET_WEIGHT_LIMIT:
        logger.info(f'权重 {weight} > {TWEET_WEIGHT_LIMIT}，按长推（CreateNoteTweet）发送')
    success, msg, res_json = XWriteAPI.post_tweet(
        auth, text, images=args.image, reply_to=args.reply_to,
        quote_url=args.quote, note=note)
    if not success:
        logger.error(f'发布失败：{msg}')
        return 1
    work_id = XWriteAPI.extract_tweet_id(res_json)
    logger.info(f'发布成功 https://x.com/i/status/{work_id}')
    return 0


def cmd_thread(auth, args):
    texts = list(args.texts or [])
    if args.file:
        # 文件里用单独一行 --- 分隔每条推文
        texts += [part.strip() for part in
                  re.split(r'^\s*---\s*$', _read_text(None, args.file), flags=re.M)
                  if part.strip()]
    if len(texts) < 2:
        logger.error('thread 至少要两条：多个 text 参数，或 --file 里用 --- 分隔')
        return 1
    success, msg, tweet_ids = XWriteAPI.post_thread(
        auth, texts, reply_to=args.reply_to, interval=args.interval)
    for tweet_id in tweet_ids:
        logger.info(f'https://x.com/i/status/{tweet_id}')
    if not success:
        logger.error(f'thread 中断：{msg}')
        return 1
    logger.info(f'thread 发布成功，共 {len(tweet_ids)} 条')
    return 0


def cmd_article(auth, args):
    markdown = _read_text(None, args.file)
    success, msg, info = XArticleAPI.post_article(
        auth, markdown, title=args.title, cover=args.cover,
        publish=args.publish, tweet_text=args.caption or '',
        reply_mode=args.reply_mode, article_id=args.article_id,
        base_dir=os.path.dirname(os.path.abspath(args.file)))
    if info.get('edit_url'):
        logger.info(f"草稿 {info['edit_url']}")
    if not success:
        logger.error(f'文章失败：{msg}')
        return 1
    if args.publish:
        logger.info(f"文章已发布 https://x.com/i/status/{info['tweet_id']}")
    else:
        logger.info('已存为草稿，去网页上预览确认后再发布（或加 --publish）')
    return 0


def cmd_article_list(auth, args):
    res_json = XArticleAPI.list_articles(
        auth, lifecycle='Published' if args.published else 'Draft',
        count=args.count)
    slice_ = res_json['data']['user']['result'].get('articles_article_mixer_slice') or {}
    items = slice_.get('items') or []
    for item in items:
        result = ((item or {}).get('article_entity_results') or {}).get('result') or {}
        logger.info(f"{result.get('rest_id')}  {result.get('title')!r}  "
                    f"{(result.get('lifecycle_state') or {}).get('lifecycle')}")
    logger.info(f'共 {len(items)} 篇')
    return 0


def cmd_article_delete(auth, args):
    _dump(XArticleAPI.delete(auth, args.article_id))
    return 0


def cmd_delete(auth, args):
    _dump(XWriteAPI.delete_tweet(auth, args.work))
    return 0


def cmd_spider(auth, args):
    _, base_path = init()
    screen_name = parse_screen_name(args.name)
    user_id = XAPI.get_user_id(auth, screen_name)

    works, cursor = [], None
    for _ in range(args.pages):
        res_json = XAPI.get_user_post_note(auth, user_id, cursor=cursor)
        works.extend(handle_work_info(item)
                     for item in extract_tweet_entries(res_json))
        next_cursor = extract_cursor(res_json, 'Bottom')
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    logger.info(f'@{screen_name} 抓到 {len(works)} 条作品')
    if args.save in ('all', 'media', 'media-image', 'media-video'):
        for work in works:
            download_work(work, base_path['media'], args.save, auth.proxies)
    if args.save in ('all', 'excel'):
        path = os.path.join(base_path['excel'], f'{screen_name}.xlsx')
        save_to_xlsx(works, path)
        logger.info(f'已写入 {path}')
    return 0


def cmd_refresh_graphql(auth, args):
    from utils.graphql_registry import refresh
    registry = refresh(proxies=auth.proxies, auth=auth)
    logger.info(f"注册表更新完成：{len(registry['operations'])} 个操作")
    return 0


def cmd_refresh_castle(auth, args):
    from utils import castle_refresh
    if args.check:
        report = castle_refresh.check(proxies=auth.proxies)
        return 1 if report['drifted'] else 0
    try:
        meta = castle_refresh.refresh(proxies=auth.proxies)
    except Exception as exc:
        logger.error(f'{type(exc).__name__}: {exc}')
        return 1
    logger.info(f"castle 生成器已刷新：公钥={meta['public_key']} token前缀={meta['token_prefix']}")
    return 0


_AUTH_FAILURE_CODES = {32, 89, 215, 326}


def _saved_cookie_is_valid(auth) -> bool:
    """用一个真实 Viewer 请求确认本地 auth_token 仍可用。"""
    if not auth.auth_token:
        return False
    try:
        # Cookie reuse must not depend on a fresh app-shell scrape.  The
        # checked-in public XCTID materials are enough for this validation
        # request and keep the offline/pure-Python path deterministic.
        if getattr(auth, '_transaction', None) is None:
            from utils.transaction import ClientTransaction
            auth._transaction = ClientTransaction.from_cached()
        result = XAPI.get_viewer(auth)
    except GraphQLError as exc:
        if exc.code in _AUTH_FAILURE_CODES:
            return False
        message = str(exc).lower()
        if any(marker in message for marker in
               ('unauthorized', 'authentication', 'expired token',
                'invalid token', 'not authorized')):
            return False
        raise
    except Exception as exc:
        # curl_cffi and requests both attach the HTTP response to transport
        # errors.  Only clear credentials for an explicit auth status; a
        # timeout or proxy failure must not destroy a still-valid cookie.
        response = getattr(exc, 'response', None)
        if response is not None and getattr(response, 'status_code', None) in (401, 403):
            return False
        raise
    try:
        return bool(result['data']['viewer']['user_results']['result']['rest_id'])
    except (KeyError, TypeError):
        return False


def cmd_login(auth, args):
    from x_apis.login_api import LoginChallenge, XJetfuelLoginApi

    force = bool(args.force or
                 (os.getenv('X_LOGIN_FORCE') or '').strip().lower()
                 in ('1', 'true', 'yes'))
    if not force and auth.auth_token:
        try:
            if _saved_cookie_is_valid(auth):
                logger.info('本地登录凭证仍有效，已复用 cookie；如需重登请使用 --force')
                return 0
        except Exception as exc:
            logger.error(f'校验本地 cookie 失败，未清除凭证：{type(exc).__name__}: {exc}')
            return 1
        clear_cookies()
        logger.info('本地 cookie 已过期，已清除，开始纯 Python 重登')

    username = args.username or os.getenv('X_USERNAME')
    password = args.password or os.getenv('X_PASSWORD')
    if not username or not password:
        logger.error('缺少用户名/密码：命令行传入或写进 .env')
        return 1

    # 密码登录默认使用干净会话；把 .env 里的 auth_token/twid/ct0 再塞进
    # begin_login 会形成“已登录 cookie + 密码流”的混合状态，服务端会拒绝，
    # 也无法验证纯算登录。需要专门做浏览器 cookie 对拍时显式设置
    # X_LOGIN_BROWSER_COOKIES=1。
    browser_cookies = None
    visible_override = (os.getenv('X_LOGIN_VISIBLE_COOKIES') or '').strip()
    visible_file = (os.getenv('X_LOGIN_VISIBLE_COOKIES_FILE') or '').strip()
    if not visible_override and visible_file:
        try:
            with open(visible_file, encoding='utf-8') as fp:
                visible_override = fp.read().strip()
            if visible_override.startswith('"'):
                visible_override = json.loads(visible_override)
        except (OSError, ValueError, TypeError):
            visible_override = ''
    if visible_override:
        captured = trans_cookies(visible_override)
        browser_cookies = {
            str(key): str(value) for key, value in captured.items()
            if str(key) not in {'auth_token', 'twid', 'ct0'} and value
        }
    elif (not force and
          (os.getenv('X_LOGIN_BROWSER_COOKIES') or '').strip().lower()
          in ('1', 'true', 'yes')):
        browser_cookies = auth.cookie
    pure_rl_values = None
    pure_rl_path = args.pure_rl or os.getenv('CASTLE_PURE_RL_FILE')
    # The CLI is a pure-computation entry point by default.  The old Node
    # runner remains available only through an explicit compatibility switch;
    # supplying a profile always wins and stays strict even if that switch is
    # accidentally present.
    pure_required = (not args.compat_node) or bool(pure_rl_path) or args.pure_only or (
        (os.getenv('CASTLE_PURE_ONLY') or '').strip().lower()
        in ('1', 'true', 'yes'))
    if pure_rl_path:
        try:
            with open(pure_rl_path, encoding='utf-8') as fp:
                pure_rl_values = json.load(fp)
            # DevTools evaluate_script exports a returned JSON object as a
            # JSON string one level deeper.  Unwrap that shape before handing
            # the profile to the strict Python Castle path.
            for _ in range(3):
                if not isinstance(pure_rl_values, str):
                    break
                try:
                    pure_rl_values = json.loads(pure_rl_values)
                except (TypeError, ValueError):
                    break
            if isinstance(pure_rl_values, dict) and 'rl' in pure_rl_values:
                if not isinstance(pure_rl_values['rl'], (dict, list, tuple)):
                    raise ValueError('纯算 profile 的 rl 必须是对象或数组')
            logger.info('已启用显式纯算 Castle 路径：{}（要求同一页面的完整 Rl 或 serialized raw profile；旧版 632 槽/当前 749 槽）',
                        pure_rl_path)
            # A browser capture may carry the same page's visible cookie
            # header.  Reuse it for the jetfuel transport when the caller did
            # not explicitly request .env auth cookies; never mix auth_token,
            # twid, or ct0 into a fresh password flow.
            if browser_cookies is None and isinstance(pure_rl_values, dict):
                captured = (pure_rl_values.get('browser_cookies')
                            or pure_rl_values.get('cookie'))
                if isinstance(captured, str):
                    captured = trans_cookies(captured)
                if isinstance(captured, dict):
                    browser_cookies = {
                        str(k): str(v) for k, v in captured.items()
                        if str(k) not in {'auth_token', 'twid', 'ct0'} and v
                    }
        except (OSError, ValueError, TypeError) as exc:
            logger.error(f'读取纯算 profile 文件失败：{type(exc).__name__}: {exc}')
            return 1
    try:
        api = XJetfuelLoginApi(proxies=auth.proxies,
                               browser_cookies=browser_cookies,
                               pure_rl_values=pure_rl_values,
                               pure_required=pure_required)
    except (OSError, ValueError, TypeError) as exc:
        logger.error(f'{type(exc).__name__}: {exc}')
        return 1
    try:
        logged_auth = api.login(username, password)
    except LoginChallenge as exc:
        logger.warning(f'登录需要人工验证：{exc}')
        logger.info('账号触发二次验证/人机验证（jetfuel 挑战），当前需人工在浏览器完成。')
        return 1
    except Exception as exc:
        logger.error(f'{type(exc).__name__}: {exc}')
        logger.info('若是限流请退避几分钟重试；也可浏览器登录后把 cookie 串写进 .env 的 X_COOKIES。')
        return 1
    save_cookies(logged_auth.cookie_str)
    logger.info(f'登录成功（user_id={logged_auth.user_id}），凭据已写回 .env 的 X_COOKIES')
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog='main.py', description='XApis 命令行入口')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('whoami', help='校验当前会话').set_defaults(func=cmd_whoami)

    p = sub.add_parser('detail', help='作品详情')
    p.add_argument('work')
    p.set_defaults(func=cmd_detail)

    p = sub.add_parser('user', help='用户信息')
    p.add_argument('name')
    p.set_defaults(func=cmd_user)

    p = sub.add_parser('search', help='搜索作品')
    p.add_argument('query')
    p.add_argument('--product', default='Top', choices=['Top', 'Latest', 'People',
                                                        'Media'])
    p.set_defaults(func=cmd_search)

    p = sub.add_parser('notifications', help='读取通知（不更新已读状态）')
    p.add_argument('--type', default='All', choices=NOTIFICATION_TYPES)
    p.add_argument('--count', type=int, default=20)
    p.add_argument('--cursor', help='上一页返回的 next_cursor')
    p.set_defaults(func=cmd_notifications)

    p = sub.add_parser('watch-users', help='持续监听指定用户新原创/引用推文（SDK 回调或 JSONL 输出）')
    p.add_argument('names', nargs='+', help='用户名、@用户名或主页 URL；无需被监听者授权')
    p.add_argument('--env', default='.env', help='采集端配置文件路径')
    p.add_argument('--guest', action='store_true', help='访客读取公开推文，完全不使用 X 登录 Cookie（覆盖范围待长跑验收）')
    p.add_argument('--interval', type=float, default=60, help='首页检查目标间隔，至少 3 秒；实际受请求预算和延迟影响')
    p.add_argument('--max-pages', type=int, default=5, help='每个补拉任务最多读取的页数')
    p.add_argument('--concurrency', type=int, default=3, help='总工作槽，2～16；其中一个专用于补拉')
    p.add_argument('--max-rps', type=float, default=1, help='来源页请求启动速率上限，默认每秒 1 次')
    p.add_argument('--scan-page-limit', type=int, default=20, help='一次补拉扫描总页数上限；达到后标记覆盖受限')
    p.add_argument('--rescan-interval', type=float, default=300, help='完成或达到上限后重新扫描历史的间隔秒数')
    p.add_argument('--duration', type=float, help='运行指定秒数后退出，例如 86400 表示 24 小时')
    p.add_argument('--state-dir', help='独立状态目录；相对路径按配置文件所在目录解析')
    p.add_argument('--metrics-file', help='持续原子更新指标 JSON 文件，不含推文正文或凭证')
    p.add_argument('--reference-posts', help='独立对照清单 JSON：[{"user_id":"...","post_id":"..."}]')
    p.add_argument('--once', action='store_true', help='完成一轮到期检查及投递后退出')
    p.add_argument('--status-only', action='store_true', help='查看所选目标状态，不采集或投递')
    p.add_argument('--retry-failed', action='store_true', help='显式重放已耗尽重试的失败事件')
    p.add_argument('--webhook', action='store_true', help='使用本地配置的 Webhook，替代 stdout 事件输出')
    p.set_defaults(func=cmd_watch_users)

    p = sub.add_parser('chat-status', help='只读检查 X Chat 公钥注册及本地身份状态')
    p.add_argument('--state', help='可选的本地账号身份文件')
    p.set_defaults(func=cmd_chat_identity)

    for command in ('chat-setup', 'chat-unlock'):
        p = sub.add_parser(command, allow_abbrev=False,
                           help='首次设置 PIN' if command == 'chat-setup' else '用 PIN 恢复既有身份')
        p.add_argument('--state', required=True, help='账号独立的私有身份文件（0600）')
        p.add_argument('--apply', action='store_true', help='默认仅检查状态；指定后才执行')
        p.add_argument('--pin-file', help='可选的 0600 PIN 文件；默认终端隐藏输入')
        if command == 'chat-setup':
            p.add_argument('--resume', action='store_true', help='核对并继续同一身份的中断流程')
        else:
            p.add_argument('--key-version', help='有多个公钥版本时必须指定')
        p.set_defaults(func=cmd_chat_identity)

    p = sub.add_parser('chat-session', help='用已恢复身份解包已有单聊密钥，输出 chat-send 的 keys 文件')
    p.add_argument('conversation')
    p.add_argument('--state', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--signature-version', required=True, type=int, choices=range(1, 12), help='经核对的当前协议版本')
    p.add_argument('--contents-version', type=int, choices=(0, 1), default=0)
    p.set_defaults(func=cmd_chat_session)

    p = sub.add_parser('message', help='统一私信入口：新会话/续发/加密/回读（默认只读预检）')
    p.add_argument('username')
    p.add_argument('text', nargs='?')
    p.add_argument('--file')
    p.add_argument('--request-id', help='业务唯一请求号；重复调用仅核对原消息')
    p.add_argument('--mode', choices=('auto','encrypted','plaintext'),default='auto')
    p.add_argument('--apply', action='store_true')
    p.set_defaults(func=cmd_message)

    p = sub.add_parser('message-status', help='仅回读核对原发送，不重新发送')
    p.add_argument('request_id')
    p.set_defaults(func=cmd_message)

    p = sub.add_parser('messages', help='读取并解析单聊消息')
    p.add_argument('username')
    p.add_argument('--limit',type=int,default=20)
    p.add_argument('--before')
    p.set_defaults(func=cmd_message)

    p = sub.add_parser('chat-request', help='发起未加密的首次文字消息请求（默认只读检查）')
    p.add_argument('username')
    p.add_argument('text', nargs='?')
    p.add_argument('--file', help='正文文件')
    p.add_argument('--receipt', help='新的发送回执；apply 时必填')
    p.add_argument('--apply', action='store_true', help='实际发送一次并回读确认')
    p.add_argument('--resume', action='store_true', help='仅回读原消息，绝不重发')
    p.set_defaults(func=cmd_chat_request)

    p = sub.add_parser('chat-start', help='创建新加密单聊并回读密钥（默认只读检查）')
    p.add_argument('username')
    p.add_argument('--state', required=True, help='已恢复的账号身份文件')
    p.add_argument('--out', required=True, help='新的会话密钥输出文件')
    p.add_argument('--receipt', help='新的创建回执文件；apply 必填')
    p.add_argument('--apply', action='store_true', help='实际分发会话密钥')
    p.add_argument('--resume', action='store_true', help='只回读原提交并恢复输出，绝不重新提交')
    p.set_defaults(func=cmd_chat_start)

    p = sub.add_parser('post', help='发作品（超过 280 权重自动按长推发）')
    p.add_argument('text', nargs='?')
    p.add_argument('--file', help='从文件读正文，适合长推')
    p.add_argument('--image', action='append', help='可重复，最多 4 张')
    p.add_argument('--reply-to', dest='reply_to')
    p.add_argument('--quote')
    p.add_argument('--note', action='store_true',
                   help='强制按长推（CreateNoteTweet）发，默认按权重自动判断')
    p.set_defaults(func=cmd_post)

    p = sub.add_parser('chat-send', help='新 X Chat 私信（默认仅本地准备）')
    p.add_argument('username')
    p.add_argument('text', nargs='?')
    p.add_argument('--file', help='从文件读取私信正文')
    p.add_argument('--keys', required=True, help='当前发送账号及会话的本地密钥文件')
    p.add_argument('--apply', action='store_true', help='校验双方账号后实际发送一次')
    p.add_argument('--receipt', help='新的发送回执文件；--apply 时必填，禁止复用')
    p.set_defaults(func=cmd_chat_send)

    p = sub.add_parser('thread', help='发 thread（每条回复上一条）')
    p.add_argument('texts', nargs='*')
    p.add_argument('--file', help='文件里用单独一行 --- 分隔每条')
    p.add_argument('--reply-to', dest='reply_to', help='整串挂在这条推文下面')
    p.add_argument('--interval', type=float, default=2.0, help='两条之间间隔秒数')
    p.set_defaults(func=cmd_thread)

    p = sub.add_parser('article', help='从 Markdown 写文章（默认只存草稿）')
    p.add_argument('file', help='Markdown 文件；第一行 `# 标题` 会被拆成文章标题')
    p.add_argument('--title', help='文章标题，不给则取正文第一行的 # 标题')
    p.add_argument('--cover', help='封面图，建议 5:2')
    p.add_argument('--publish', action='store_true', help='写完直接发布')
    p.add_argument('--caption', help='发布时附带的推文说明文字（≤256 字）')
    p.add_argument('--reply-mode', dest='reply_mode', choices=REPLY_MODES,
                   help='谁可以回复，默认所有人')
    p.add_argument('--article-id', dest='article_id', help='覆盖更新这篇已有草稿')
    p.set_defaults(func=cmd_article)

    p = sub.add_parser('article-list', help='列出文章草稿 / 已发布')
    p.add_argument('--published', action='store_true')
    p.add_argument('--count', type=int, default=20)
    p.set_defaults(func=cmd_article_list)

    p = sub.add_parser('article-delete', help='删除文章（不可撤销）')
    p.add_argument('article_id')
    p.set_defaults(func=cmd_article_delete)

    p = sub.add_parser('delete', help='删除作品')
    p.add_argument('work')
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser('spider', help='抓取用户作品并落盘')
    p.add_argument('name')
    p.add_argument('--pages', type=int, default=2)
    p.add_argument('--save', default='excel',
                   choices=['all', 'excel', 'media', 'media-image', 'media-video',
                            'none'])
    p.set_defaults(func=cmd_spider)

    sub.add_parser('refresh-graphql', help='从线上前端重建 static/graphql.json') \
        .set_defaults(func=cmd_refresh_graphql)

    p = sub.add_parser('refresh-castle',
                       help='从线上拉 castle SDK 重打补丁（纯算）；--check 只做漂移检测')
    p.add_argument('--check', action='store_true', help='只读漂移检测，不改文件')
    p.set_defaults(func=cmd_refresh_castle)

    p = sub.add_parser('login', help='用户名+密码登录（web jetfuel 流）')
    p.add_argument('--username')
    p.add_argument('--password')
    p.add_argument('--pure-rl', metavar='FILE',
                   help='同一浏览器采集的完整 Rl 或 serialized raw profile JSON（旧版 632 槽/当前 749 槽）')
    p.add_argument('--pure-only', action='store_true',
                   help='强制纯算；没有 --pure-rl/profile 时直接失败，绝不回退 Node')
    p.add_argument('--compat-node', action='store_true',
                   help='显式启用旧 Node Castle runner（仅兼容，不属于纯算验收）')
    p.add_argument('--force', action='store_true',
                   help='忽略本地 cookie，强制重新走纯 Python 账密登录')
    p.set_defaults(func=cmd_login)

    return parser


def main():
    args = build_parser().parse_args()
    if args.command in ('message','messages','message-status','watch-users'):
        return args.func(None,args)
    if args.command == 'chat-send' and not args.apply:
        return args.func(None, args)
    auth, _ = init()
    return args.func(auth, args)


if __name__ == '__main__':
    sys.exit(main())
