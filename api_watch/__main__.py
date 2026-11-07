"""python -m api_watch: standalone monitor and offline status commands."""
import argparse
import json
import os
from pathlib import Path
import signal
import threading

from api_watch.config import ConfigError, load_config, load_secrets
from api_watch.runner import emit, healthy, run
from api_watch.state import Store, process_lock


def main(argv=None):
    parser = argparse.ArgumentParser(description='XApis 接口覆盖巡检（读取、受控写入、手动生命周期）')
    parser.add_argument('--config', help='巡检 JSON 配置；省略使用内置默认值')
    parser.add_argument('--env-file', help='显式凭据配置；不会自动读取项目 .env')
    parser.add_argument('--state-dir', default=os.environ.get('API_WATCH_STATE_DIR', '.api-watch'))
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('once', 'run'):
        sub = commands.add_parser(command, help='单次检查' if command == 'once' else '持续巡检')
        choices = ('all', 'scan', 'probes', 'writes', 'lifecycle') if command == 'once' else ('all', 'scan', 'probes', 'writes')
        sub.add_argument('--only', choices=choices, default='all')
    commands.add_parser('status', help='读取持久状态，不请求 X')
    commands.add_parser('health', help='检查进程心跳，供容器健康检查使用')
    commands.add_parser('inventory', help='列出当前代码引用的 GraphQL 操作，不请求 X')
    commands.add_parser('check-config', help='检查非敏感配置，不读取凭据或请求 X')
    initialize = commands.add_parser('init', help='创建带模板和图片/视频样本的独立监控目录')
    initialize.add_argument('--directory', required=True)
    coverage_parser = commands.add_parser('coverage', help='列出逐接口参数缺口和当前配置下的执行证据')
    coverage_parser.add_argument('--strict', action='store_true', help='只要有未通过/未配置项则退出 2')
    commands.add_parser('journal', help='读取未确认写入和待清理资源')
    cleanup_parser = commands.add_parser('cleanup', help='只清理指定历史运行的已登记测试资源')
    cleanup_parser.add_argument('--run-id', required=True)
    acknowledge = commands.add_parser('acknowledge-run', help='人工核对后解除写入暂停；不改变探针失败证据')
    acknowledge.add_argument('--run-id', required=True)
    acknowledge.add_argument('--note', required=True, help='人工核对结论，不要填写凭据或消息内容')
    contract = commands.add_parser('capture-settings-contract', help='只读采集聊天设置字段/类型供人工审阅')
    contract.add_argument('--out', required=True)
    inspect_account = commands.add_parser('inspect-account', help='只读核对 Cookie 身份并查询测试 peer 的数字 ID')
    inspect_account.add_argument('--peer', help='明确的第二个测试账号用户名')
    accept = commands.add_parser('accept-baseline', help='将指定完整观测设为比较基线；不更新业务接口')
    accept.add_argument('--scope', required=True)
    accept.add_argument('--digest', required=True)
    resources = commands.add_parser('accept-resources', help='确认已观察的入口资源版本，不确认接口兼容性')
    resources.add_argument('--scope', required=True)
    resources.add_argument('--fingerprint', required=True)
    operations = commands.add_parser('accept-operations', help='确认已观察操作的比较基线，不推断未观察接口')
    operations.add_argument('--scope', required=True)
    operations.add_argument('--fingerprint', required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == 'init':
            from api_watch.scaffold import initialize
            emit(initialize(args.directory))
            return 0
        if args.command == 'health':
            return 0 if healthy(args.state_dir) else 1
        if args.command == 'inventory':
            from api_watch.catalog import inventory
            from api_watch.manifest import registry
            emit({'graphql_definitions': inventory(), 'executable_probes': registry()})
            return 0
        if args.command == 'journal':
            from api_watch.recovery import journal
            store = Store(args.state_dir)
            try:
                emit({'runs': journal(store)})
            finally:
                store.close()
            return 0
        if args.command == 'acknowledge-run':
            from api_watch.recovery import journal, acknowledge
            with process_lock(args.state_dir):
                store = Store(args.state_dir)
                try:
                    acknowledge(store, args.run_id, args.note)
                    emit({'summary': '已记录人工核对并解除该运行暂停；原失败证据保持不变'})
                finally:
                    store.close()
            return 0
        if args.command == 'status':
            if not (Path(args.state_dir) / 'state.sqlite3').is_file():
                emit({'checks': [], 'snapshots': [], 'pending_alerts': 0, 'healthy': False})
                return 0
            store = Store(args.state_dir)
            try:
                emit(dict(store.report(), healthy=healthy(args.state_dir)))
            finally:
                store.close()
            return 0
        if args.command == 'accept-baseline':
            with process_lock(args.state_dir):
                store = Store(args.state_dir)
                try:
                    store.accept_baseline(args.scope, args.digest)
                finally:
                    store.close()
            emit({'summary': '观测基线已更新；业务接口文件未修改'})
            return 0
        if args.command == 'accept-resources':
            with process_lock(args.state_dir):
                store = Store(args.state_dir)
                try:
                    if store.get_meta('resource_latest:' + args.scope) != args.fingerprint:
                        raise ValueError('只能确认该范围最近一次观测的资源版本')
                    store.set_meta('resource_baseline:' + args.scope, args.fingerprint)
                finally:
                    store.close()
            emit({'summary': '入口资源比较基线已更新；不代表接口兼容性验收'})
            return 0
        if args.command == 'accept-operations':
            with process_lock(args.state_dir):
                store = Store(args.state_dir)
                try:
                    latest = store.get_meta('operations_latest:' + args.scope, {})
                    if latest.get('fingerprint') != args.fingerprint or not latest.get('operations'):
                        raise ValueError('只能确认该范围最近观察的操作版本')
                    store.set_meta('operations_baseline:' + args.scope, latest['operations'])
                    store.set_meta('operations_changes:' + args.scope, {})
                finally:
                    store.close()
            emit({'summary': '已观察操作的比较基线已更新；业务接口文件未修改'})
            return 0
        config = load_config(args.config)
        if args.command == 'check-config':
            emit({'valid': True, 'config': config})
            return 0
        if args.command == 'coverage':
            from api_watch.manifest import coverage
            secrets = load_secrets(args.env_file)
            store = Store(args.state_dir)
            try:
                report = coverage(config, secrets, store)
                emit(report)
                return 2 if args.strict and not report['all_passed'] else 0
            finally:
                store.close()
        stopped = threading.Event()
        # Only this dedicated CLI process is affected. Avoid hidden host proxy
        # inheritance in the reused SDK transport; API_WATCH_PROXY is explicit.
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'):
            os.environ.pop(key, None)
            os.environ.pop(key.lower(), None)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stopped.set())
        if args.command in ('cleanup', 'capture-settings-contract', 'inspect-account'):
            if args.command == 'capture-settings-contract' and Path(args.out).exists():
                emit({'type': 'error', 'summary': '契约输出文件已存在，拒绝覆盖；请选择新的候选文件'})
                return 2
            secrets = load_secrets(args.env_file)
            with process_lock(args.state_dir):
                store = Store(args.state_dir)
                try:
                    if args.command == 'cleanup':
                        from api_watch.recovery import cleanup
                        rows = cleanup(config, secrets, store, args.run_id, stopped)
                        for row in rows:
                            store.record(row, config['failure_threshold'])
                            emit(row)
                        return 0 if rows and all(row['status'] == 'ok' for row in rows) else 2
                    from api_watch.execution import ProbeContext
                    from api_watch.suite import _authenticate
                    from api_watch.read_probes import derive_settings_contract
                    from x_apis.x_dm_api import XChatAPI
                    ctx = ProbeContext(config, secrets, store, stop_event=stopped)
                    try:
                        if not _authenticate(ctx):
                            emit({'summary': '账号预检未通过，未执行后续请求'})
                            return 2
                        if args.command == 'inspect-account':
                            from x_apis.x_api import XAPI
                            from api_watch.probes import _user
                            info = {'test_account_id': ctx.actor_id}
                            if args.peer:
                                import re
                                if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', args.peer):
                                    raise ValueError('测试账号用户名无效')
                                raw = XAPI.get_user_info(ctx.auth, args.peer)
                                _user(raw)
                                peer = str(raw['data']['user']['result']['rest_id'])
                                if peer == ctx.actor_id:
                                    raise ValueError('测试接收账号必须不同于当前账号')
                                info.update(peer_user_id=peer, peer_username=args.peer,
                                            conversation_id_format=':'.join(sorted((peer, ctx.actor_id), key=int)))
                            emit({'samples': info, 'summary': '账号已核对；会话 ID 格式不证明已建立会话，请确认后配置'})
                            return 0
                        candidate = derive_settings_contract(XChatAPI.get_dm_settings(ctx.auth))
                        target = Path(args.out)
                        # Never overwrite a previously reviewed baseline.
                        with target.open('x', encoding='utf-8') as fp:
                            json.dump(candidate, fp, ensure_ascii=False, indent=2)
                            fp.write('\n')
                        emit({'summary': '字段/类型候选已保存，请审阅后配置 chat_settings_contract_file',
                              'field_count': len(candidate)})
                        return 0
                    finally:
                        ctx.finish()
                finally:
                    store.close()
        return run(config, args.state_dir, lambda: load_secrets(args.env_file),
                   once=args.command == 'once', only=args.only, stop_event=stopped)
    except (ConfigError, ValueError, RuntimeError):
        emit({'type': 'error', 'summary': '配置、状态或进程锁无效；检查配置和状态目录，确认没有重复实例'})
        return 2
    except Exception:
        emit({'type': 'error', 'summary': '巡检启动或状态访问失败；未输出可能包含凭据的异常详情'})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
