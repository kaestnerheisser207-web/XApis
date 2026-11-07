"""Inventory and comparison against the actual installed XApis sources."""
import ast
import json
from pathlib import Path

from utils.resource_paths import asset_path

ROOT = Path(__file__).resolve().parent.parent
CHAT_OPERATIONS = {
    'GENERATE_TOKEN_QID': 'GenerateXChatTokenMutation',
    'DM_SETTINGS_QID': 'xChatDmSettingsQuery',
    'MEMBERS_QID': 'GetUsersByIdsForXChat',
    'PUBLIC_KEYS_QID': 'GetPublicKeysQuery',
    'INITIAL_PAGE_QID': 'GetInitialXChatPageQuery',
    'CONVERSATION_PAGE_QID': 'GetConversationPageQuery',
    'SEND_MESSAGE_CREATE_QID': 'SendMessageCreateMutation',
    'ADD_PUBLIC_KEY_QID': 'AddXChatPublicKeyMutation',
    'ADD_CONVERSATION_KEYS_QID': 'AddEncryptedConversationKeysMutation',
}


def installed_operations():
    registry = json.loads(asset_path('graphql.json').read_text())
    operations = dict(registry['operations'])
    tree = ast.parse((ROOT / 'x_apis/x_dm_api.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in CHAT_OPERATIONS:
                    operations[CHAT_OPERATIONS[target.id]] = {'queryId': node.value.value}
    return operations


def inventory():
    operations = installed_operations()
    refs = {}
    for path in sorted((ROOT / 'x_apis').glob('*.py')):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in operations:
                refs.setdefault(node.value, []).append(f'{path.relative_to(ROOT)}:{node.lineno}')
    for name in CHAT_OPERATIONS.values():
        refs.setdefault(name, ['x_apis/x_dm_api.py'])
    return [{'operation': name, 'query_id': operations[name]['queryId'],
             'type': operations[name].get('operationType', 'query' if 'Mutation' not in name else 'mutation'),
             'sources': sorted(set(refs[name])), 'coverage': 'definition; probe coverage is reported separately'}
            for name in sorted(refs)]


def compare_installed(snapshot):
    """Only compare positively observed operations, never infer removal from absence."""
    current = installed_operations()
    used = {row['operation']: row['sources'] for row in inventory()}
    changes = {}
    for name, candidate in snapshot.get('operations', {}).items():
        if name not in current or name not in used:
            continue
        fields = {}
        for key in ('queryId', 'operationType', 'featureSwitches', 'fieldToggles'):
            if key not in current[name] or key not in candidate:
                continue
            before, after = current[name][key], candidate[key]
            if isinstance(before, list):
                before, after = sorted(before), sorted(after)
            if before != after:
                fields[key] = {'before': before, 'after': after}
        if fields:
            changes[name] = {'fields': fields, 'sources': used[name]}
    return changes
