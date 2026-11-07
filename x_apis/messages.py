"""Business-level messages: account setup, pagination, encryption and reconciliation."""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from utils.xchat_codec import decode_base64_struct, decode_struct, field, message_event_summary
from utils.xchat_conversation import inspect_recipient, read_conversation_events, start_conversation
from utils.xchat_crypto import load_key_material, prepare_message_request, prepare_text_message
from utils.xchat_identity import (IdentityError, _locked, _read, _same_public, _save,
                                  _viewer, public_records, sdk_call, setup_identity, unlock_identity)
from utils.xchat_identity import export_conversation_material
from utils.x_util import parse_screen_name
from x_apis.client_errors import XClientError, api_method
from x_apis.x_dm_api import XChatAPI


def _private_json(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536:
            raise XClientError('invalid_state', '状态文件必须是 0600 小型普通文件')
        return json.load(f)


def _directory(path):
    if path.is_symlink():
        raise XClientError('invalid_state', '状态目录不能是符号链接')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _create(encoded):
    return field(field(decode_base64_struct(encoded), 7, 12), 1, 12)


class Messages:
    def __init__(self, client):
        self.client = client

    @property
    def auth(self):
        return self.client.auth

    def _account(self):
        uid, _ = _viewer(self.auth)
        root = _directory(self.client.state_dir)
        return uid, _directory(root / uid)

    def _identity_path(self, uid, directory):
        configured = self.client.identity_path
        return Path(str(configured).replace('{user_id}', uid)) if configured else directory / 'identity.json'

    @api_method
    def identity_status(self):
        from utils.xchat_identity import inspect_identity
        uid, directory = self._account()
        return inspect_identity(self.auth, self._identity_path(uid,directory))

    @api_method
    def ensure_identity(self, *, pin=None, key_version=None, resume_setup=False, retry_unlock=False):
        """只为无服务端公钥的当前账号自动设置默认 PIN；已有账号绝不试默认 PIN。"""
        uid, directory = self._account()
        path = self._identity_path(uid,directory)
        with _locked(directory / 'identity-operation'):
            records = public_records(self.auth,uid)
            if path.exists():
                state = _read(path)
                if state['sender_id'] != uid:
                    raise XClientError('identity_mismatch', '身份文件属于另一个账号')
                if state['phase'] != 'ready':
                    if not resume_setup:
                        raise XClientError('identity_incomplete', '初始化曾中断；用 ensure_identity(resume_setup=True) 核对恢复')
                    return setup_identity(self.auth, pin or self.client.default_pin, path, resume=True)
                if not any(r['public_key_with_metadata']['version'] == state['version'] and
                           _same_public(r['public_key_with_metadata']['public_key'],state['registration']) for r in records):
                    raise XClientError('identity_mismatch','本地身份与服务端公钥不匹配，不会重置')
                validated = sdk_call('validate',key_blob_b64=state['key_blob_b64'],version=state['version'])
                if not _same_public(validated,state['registration']):
                    raise XClientError('identity_mismatch','本地私钥不匹配')
                return {'state':'ready','sender_id':uid,'version':state['version'],'created':False}
            if not records:
                selected = pin or self.client.default_pin
                if not selected:
                    raise XClientError('default_pin_required','首次初始化需要 X_CHAT_DEFAULT_PIN 或 pin 参数')
                try:
                    return setup_identity(self.auth,selected,path)
                except IdentityError as exc:
                    raise XClientError('identity_setup_failed',str(exc)) from None
            selected = pin or self.client.existing_pin
            if not selected:
                raise XClientError('existing_pin_required','账号已注册；请传原 PIN 恢复，默认 PIN 不用于已有账号')
            marker = directory / 'unlock-attempt.json'
            if marker.exists() and _private_json(marker).get('status') != 'ready' and not retry_unlock:
                raise XClientError('unlock_needs_attention','上次恢复未确认；核对 PIN 后显式 retry_unlock=True，不自动重复尝试')
            if key_version is None and len(records) != 1:
                raise XClientError('key_version_required','存在多个身份版本，恢复时必须指定 key_version')
            _save(marker,{'status':'attempting','sender_id':uid,'key_version':key_version})
            result = unlock_identity(self.auth,selected,path,version=key_version)
            _save(marker,{'status':'ready','sender_id':uid,'key_version':result['version']})
            return result

    def _conversation(self, recipient):
        uid,directory = self._account()
        member = inspect_recipient(self.auth,recipient)
        if member['recipient_id'] == uid:
            raise XClientError('invalid_recipient','仅支持两个不同账号的单聊')
        cid = ':'.join(sorted((uid,member['recipient_id']),key=int))
        return uid,directory,member,cid

    def _keys(self, uid, directory, cid, events):
        from utils.xchat_codec import field as get
        versions = [get(get(get(decode_base64_struct(e),7,12),3,12),1,11).decode()
                    for e in events if message_event_summary(e)['event_type']==3]
        if not versions:
            raise XClientError('conversation_key_missing','会话已加密但未取得密钥事件，拒绝降级')
        version = max(versions,key=int)
        path = directory / ('conversation-' + _hash(cid+':'+version) + '.json')
        if not path.exists():
            export_conversation_material(self.auth,self._identity_path(uid,directory),cid,path,
                                         signature_version=7,expected_key_version=version,events=events)
        material = load_key_material(path)
        if material.sender_id != uid or material.conversation_id != cid or str(material.conversation_key_version)!=version:
            raise XClientError('invalid_state','会话密钥状态不匹配')
        return material

    def _read_items(self, uid, directory, cid, events):
        items, encrypted_events = [], []
        for encoded in events:
            summary = message_event_summary(encoded)
            if summary['event_type']==3:
                encrypted_events.append(encoded)
            elif summary['event_type']==1:
                create = _create(encoded)
                if 101 in create:
                    encrypted_events.append(encoded)
                    continue
                try:
                    holder = decode_struct(field(create,100,11))
                    text = field(field(field(holder,1,12),1,12),1,11).decode()
                    items.append({**summary,'text':text,'encrypted':False,'signature_verified':False})
                except (ValueError,TypeError,AttributeError,UnicodeError):
                    items.append({**summary,'text':None,'encrypted':False,'error':'unsupported_contents'})
        if any(message_event_summary(e)['event_type']==1 for e in encrypted_events):
            path = self._identity_path(uid,directory)
            if not path.exists():
                raise XClientError('existing_pin_required','读取加密消息需要先 ensure_identity(pin=原PIN)')
            state = _read(path)
            if state['sender_id']!=uid or state['phase']!='ready':
                raise XClientError('identity_mismatch','本地聊天身份未就绪')
            signing=[]
            for member in cid.split(':'):
                for record in public_records(self.auth,member,allow_managed=member!=uid):
                    metadata=record['public_key_with_metadata'];public=metadata['public_key']
                    signing.append({'user_id':member,'public_key_version':metadata['version'],
                                    'public_key':public['signing_public_key'],
                                    'identity_public_key':public['public_key'],
                                    'identity_public_key_signature':public['identity_public_key_signature']})
            decoded=sdk_call('decrypt_messages',key_blob_b64=state['key_blob_b64'],version=state['version'],
                             sender_id=uid,signing_keys=signing,events=encrypted_events)
            if decoded['error_indices']:
                raise XClientError('message_verification_failed','部分加密事件解密或验签失败')
            for event in decoded['messages']:
                if event.get('verified') is not True or event['conversation_id']!=cid:
                    raise XClientError('message_verification_failed','加密消息未通过签名校验')
                items.append({'message_id':event['id'],'sequence_id':event['sequence_id'],
                              'sender_id':event['sender_id'],'conversation_id':cid,
                              'text':event['content'].get('text'),'encrypted':True,'signature_verified':True})
        return sorted(items,key=lambda item:int(item['sequence_id']),reverse=True)

    @api_method
    def list(self, recipient, *, limit=20, before=None, max_pages=100):
        if type(limit) is not int or limit<1:
            raise ValueError('limit must be positive')
        uid,directory,member,cid=self._conversation(recipient)
        events=read_conversation_events(self.auth,cid,max_pages=max_pages)
        items=self._read_items(uid,directory,cid,events)
        if before is not None: items=[i for i in items if int(i['sequence_id'])<int(before)]
        selected=items[:limit]
        return {'conversation_id':cid,'items':selected,
                'next_cursor':selected[-1]['sequence_id'] if len(items)>limit else None}

    @api_method
    def preview(self, recipient):
        uid,directory,member,cid=self._conversation(recipient)
        events=read_conversation_events(self.auth,cid)
        encrypted=any(message_event_summary(e)['event_type']==3 or
                       (message_event_summary(e)['event_type']==1 and 101 in _create(e)) for e in events)
        return {'sender_id':uid,'conversation_id':cid,**member,'exists':bool(events),
                'mode':'encrypted' if encrypted or member['can_create_encrypted_conversation'] else 'plaintext'}

    def _receipt_path(self,directory,request_id):
        if not isinstance(request_id,str) or not request_id.strip() or len(request_id)>200:
            raise ValueError('request_id must be 1..200 characters')
        return _directory(directory/'requests') / (_hash(request_id)+'.json')

    @staticmethod
    def _result(record):
        return {k:record[k] for k in ('request_id','message_id','sender_id','recipient_id',
                                     'conversation_id','encrypted','status') if k in record}

    def _reconcile(self,uid,directory,path,record):
        if record['sender_id']!=uid:
            raise XClientError('identity_mismatch','回执不属于当前发送账号')
        if record['status']=='verified':return self._result(record)
        if record['status']=='preparing':
            return self._result(record)
        events=read_conversation_events(self.auth,record['conversation_id'])
        relevant=[e for e in events if message_event_summary(e)['event_type']==3 or
                  message_event_summary(e)['message_id']==record['message_id']]
        matches=[i for i in self._read_items(uid,directory,record['conversation_id'],relevant)
                 if i['message_id']==record['message_id']]
        if len(matches)==1:
            item=matches[0]
            if (item['sender_id']!=uid or item['encrypted']!=record['encrypted'] or
                    not isinstance(item['text'],str) or _hash(item['text'])!=record['text_sha256']):
                raise XClientError('readback_mismatch','回读消息身份、加密状态或正文不匹配',result=self._result(record))
            record['status']='verified';_save(path,record)
        return self._result(record)

    @api_method
    def reconcile(self,request_id):
        uid,directory=self._account()
        path=self._receipt_path(directory,request_id)
        with _locked(path):
            return self._reconcile(uid,directory,path,_private_json(path))

    @api_method
    def send(self, recipient, text, *, request_id=None, mode='auto', verify=True):
        """发送一次。重复 request_id 只核对原消息；正文或模式变化会报冲突。"""
        if not isinstance(text,str) or not text.strip():raise ValueError('text cannot be empty')
        if mode not in ('auto','encrypted','plaintext'):raise ValueError('mode must be auto/encrypted/plaintext')
        name=parse_screen_name(recipient)
        request_id=request_id if request_id is not None else str(uuid.uuid4())
        uid,directory=self._account()
        path=self._receipt_path(directory,request_id)
        with _locked(directory/'send-operation'), _locked(path):
            if path.exists():
                record=_private_json(path)
                if (record['recipient']!=name or record['text_sha256']!=_hash(text) or record['mode']!=mode):
                    raise XClientError('request_conflict','同一 request_id 不能更换收件人、正文或模式')
                if record['status']!='preparing':
                    return self._reconcile(uid,directory,path,record)
            else:
                record={'request_id':request_id,'sender_id':uid,'recipient':name,'mode':mode,
                        'text_sha256':_hash(text),'message_id':str(uuid.uuid4()),'status':'preparing'}
                _save(path,record)
            member=inspect_recipient(self.auth,name)
            if member['recipient_id']==uid:raise XClientError('invalid_recipient','不能向自己创建单聊')
            if not member['can_dm']:raise XClientError('dm_not_allowed',member['reason'])
            cid=':'.join(sorted((uid,member['recipient_id']),key=int))
            if record.get('recipient_id',member['recipient_id'])!=member['recipient_id']:
                raise XClientError('recipient_changed','收件人用户名已对应另一个账号')
            record.update(recipient_id=member['recipient_id'],conversation_id=cid)
            _save(path,record)
            events=read_conversation_events(self.auth,cid)
            has_keys=any(message_event_summary(e)['event_type']==3 for e in events)
            has_encrypted=has_keys or any(message_event_summary(e)['event_type']==1 and 101 in _create(e) for e in events)
            if mode=='plaintext' and has_encrypted:
                raise XClientError('encryption_downgrade','已有加密会话不能降级为未加密消息')
            encrypted=has_encrypted or mode=='encrypted' or (mode=='auto' and member['can_create_encrypted_conversation'])
            # No key records is authoritative only after a successful full registry read.
            own=public_records(self.auth,uid,allow_managed=True)
            if not own or encrypted:self.ensure_identity()
            if encrypted and not has_keys:
                if has_encrypted:raise XClientError('conversation_key_missing','加密消息缺少可恢复密钥事件')
                if not member['can_create_encrypted_conversation']:
                    raise XClientError('encryption_unavailable','对方当前只允许未加密消息请求')
                material_path=directory/('new-conversation-'+_hash(cid)+'.json')
                creation=directory/('new-conversation-'+_hash(cid)+'.receipt.json')
                start_conversation(self.auth,name,self._identity_path(uid,directory),material_path,creation,
                                   apply=True,resume=creation.exists(),upgrade_plaintext=True)
                events=read_conversation_events(self.auth,cid)
            if encrypted:
                prepared=prepare_text_message(text,self._keys(uid,directory,cid,events),message_id=record['message_id'])
            else:prepared=prepare_message_request(text,cid,message_id=record['message_id'])
            record.update(status='sending',encrypted=encrypted);_save(path,record)
            try:
                payload=XChatAPI.send_message(self.auth,**prepared.as_send_kwargs(),allow_unencrypted=not encrypted)
                returned=message_event_summary(payload['data']['xchat_send_create_message_event']['encoded_message_event'])
                if returned['sender_id']!=uid:raise ValueError('sender mismatch')
                record['status']='sent';_save(path,record)
            except Exception as exc:
                record['status']='unconfirmed';_save(path,record)
                raise XClientError('delivery_unknown','发送结果未确认，请用相同 request_id 调用 reconcile，勿换 ID 重发',
                                   result=self._result(record)) from None
            if verify:
                try:return self._reconcile(uid,directory,path,record)
                except Exception:
                    raise XClientError('readback_failed','服务端已确认发送，但回读未完成；可 reconcile 核对',result=self._result(record)) from None
            return self._result(record)
