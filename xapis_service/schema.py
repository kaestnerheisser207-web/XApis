"""OpenAPI for the internal provider, generated from its capability declaration."""
from copy import deepcopy

from xapis_service import __version__
from xapis_service.contract import CAPABILITIES


def document():
    properties = {
        'username': {'type': 'string', 'pattern': '^@?[A-Za-z0-9_]{1,15}$'},
        'content_id': {'type': 'string', 'pattern': '^[1-9][0-9]{0,29}$'},
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 2048},
        'product': {'type': 'string', 'enum': ['Top', 'Latest', 'Media'], 'default': 'Latest'},
        'count': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20},
        'cursor': {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 8192},
    }
    account = {'type': 'object', 'additionalProperties': False, 'required': ['cookies'],
               'properties': {'cookies': {'type': 'string', 'minLength': 1, 'maxLength': 32768,
                                          'writeOnly': True, 'description': 'Caller session containing auth_token and ct0.'},
                              'proxy': {'type': ['string', 'null'], 'maxLength': 4096,
                                        'writeOnly': True, 'description': 'Explicit proxy URL with port; absent means direct egress.'}}}
    variants = []
    for capability, fields in CAPABILITIES.items():
        variants.append({'type': 'object', 'additionalProperties': False,
                         'required': ['capability', 'payload', 'account'],
                         'properties': {'capability': {'const': capability}, 'account': deepcopy(account),
                                        'payload': {'type': 'object', 'additionalProperties': False,
                                                    'required': [fields[0]],
                                                    'properties': {name: deepcopy(properties[name]) for name in fields}}}})
    post_properties = {name: {'type': 'string'} for name in
                       ('work_id', 'work_url', 'work_type', 'title', 'create_time', 'lang',
                        'user_id', 'nickname', 'screen_name', 'user_url')}
    post_properties.update({name: {'type': 'integer', 'minimum': 0} for name in
                            ('like_count', 'retweet_count', 'reply_count', 'quote_count', 'bookmark_count')})
    post_properties['view_count'] = {'type': ['string', 'integer']}
    post_properties.update({name: {'type': 'array', 'items': {'type': 'string'}}
                            for name in ('image_urls', 'video_urls')})
    post = {'type': 'object', 'required': list(post_properties), 'properties': post_properties,
            'additionalProperties': False}
    profile = {'type': 'object', 'required': ['user_id', 'username', 'name', 'description', 'followers'],
               'additionalProperties': False,
               'properties': {'user_id': {'type': 'string'}, 'username': {'type': 'string'},
                              'name': {'type': ['string', 'null']}, 'description': {'type': ['string', 'null']},
                              'followers': {'type': ['integer', 'null'], 'minimum': 0}}}
    page = {'type': 'object', 'required': ['items', 'next_cursor'], 'additionalProperties': False,
            'properties': {'items': {'type': 'array', 'items': {'$ref': '#/components/schemas/Post'}},
                           'next_cursor': {'type': ['string', 'null']}}}
    error = {'type': 'object', 'required': ['error'], 'additionalProperties': False,
             'properties': {'error': {'type': 'object', 'required': ['code', 'message', 'retryable'],
                                      'additionalProperties': False,
                                      'properties': {'code': {'type': 'string'}, 'message': {'type': 'string'},
                                                     'retryable': {'const': False},
                                                     'retry_after': {'type': 'integer', 'minimum': 0},
                                                     'upstream_codes': {'type': 'array', 'items': {'type': 'integer'}}}}}}
    responses = {'200': {'description': 'SDK profile, post, or one explicit page. No automatic pagination.',
                          'content': {'application/json': {'schema': {'type': 'object', 'required': ['data'],
                                                                      'additionalProperties': False,
                                                                      'properties': {'data': {'oneOf': [
                                                                          {'$ref': '#/components/schemas/' + name}
                                                                          for name in ('Profile', 'Post', 'Page')]}}}}}}}
    for status in ('400', '401', '403', '404', '413', '429', '502', '503', '504'):
        responses[status] = {'description': 'Explicit failure; never automatically replay this invocation.',
                             'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}}
    return {'openapi': '3.1.0', 'info': {'title': 'XApis internal read provider', 'version': __version__},
            'security': [{'serviceKey': []}],
            'paths': {'/v1/execute': {'post': {'operationId': 'execute',
                       'requestBody': {'required': True, 'content': {'application/json': {'schema': {'oneOf': variants}}}},
                       'responses': responses}},
                      '/v1/capabilities': {'get': {'operationId': 'capabilities', 'responses': {'200': {'description': 'Enabled capability IDs'}}}},
                      '/healthz': {'get': {'security': [], 'responses': {'200': {'description': 'Process is serving HTTP'}}}},
                      '/readyz': {'get': {'security': [], 'responses': {'200': {'description': 'Configuration and local assets loaded; X availability is not checked'}}}}},
            'components': {'securitySchemes': {'serviceKey': {'type': 'http', 'scheme': 'bearer'}},
                           'schemas': {'Profile': profile, 'Post': post, 'Page': page, 'Error': error}}}
