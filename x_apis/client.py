"""Public Python SDK. Business parameters in; structured results or XClientError out."""
import os
from pathlib import Path

from dotenv import dotenv_values
from builder.auth import XAuth
from utils.notification_util import parse_notification_page
from utils.x_util import extract_cursor, extract_tweet_entries, parse_tweet_id
from x_apis.client_errors import XClientError, api_method, graphql_error
from x_apis.messages import Messages
from x_apis.response_validation import (
    normalized_post, required_path, validate_user, validated_profile_record, validated_timeline,
    validated_list_search_page,
)
from x_apis.x_api import XAPI
from x_apis.x_write_api import XWriteAPI
from x_apis.x_media_api import XMediaAPI
from x_apis.x_article_api import XArticleAPI


def _checked(payload):
    if not isinstance(payload,dict):raise XClientError('invalid_response','响应不是对象')
    if payload.get('errors'):raise graphql_error(payload['errors'])
    return payload


def _page(payload,kind):
    timeline=validated_timeline(_checked(payload),kind)
    return {'items':[normalized_post(e) for e in extract_tweet_entries(timeline)],
            'next_cursor':extract_cursor(timeline,'Bottom')}


class Lists:
    def __init__(self,client):self.client=client

    @api_method
    def search(self,query, *, count=20,cursor=None):
        return validated_list_search_page(XAPI.search_work(self.client.auth,query,product='Lists',count=count,cursor=cursor))


class Notifications:
    def __init__(self,client):self.client=client

    @api_method
    def list(self, *, category='All', count=20, cursor=None):
        return parse_notification_page(XAPI.get_notifications(self.client.auth,category,count,cursor))


class Users:
    def __init__(self,client):self.client=client

    def monitor(self, usernames, **options):
        """长期监听用户原创及引用推文，客户端会话由采集端统一管理。"""
        from x_apis.user_monitor import UserPostMonitor
        return UserPostMonitor(self.client, usernames, **options)

    @api_method
    def me(self):
        payload=_checked(XAPI.get_viewer(self.client.auth))
        return self._normalize(validate_user(required_path(payload,'data','viewer','user_results','result'),viewer=True))

    @staticmethod
    def _normalize(user):
        validate_user(user)
        core=user.get('core') or user.get('legacy') or {}
        return validated_profile_record({'user_id':user['rest_id'],'username':core.get('screen_name'),'name':core.get('name'),
                'description':(user.get('profile_bio') or {}).get('description'),
                'followers':(user.get('relationship_counts') or {}).get('followers')})

    @api_method
    def get(self,username):
        payload=_checked(XAPI.get_user_info(self.client.auth,username))
        return self._normalize(required_path(payload,'data','user','result'))

    @api_method
    def posts(self,username, *, count=20,cursor=None):
        uid=self.get(username)['user_id']
        return _page(XAPI.get_user_post_note(self.client.auth,uid,count=count,cursor=cursor),'user')

    @staticmethod
    def _relationship(raw,uid,following):
        _checked(raw)
        if raw.get('id_str')!=uid or raw.get('following') is not following:
            raise XClientError('follow_unconfirmed' if following else 'unfollow_unconfirmed',
                               '关注状态回执未确认，请先查询目标账号状态再决定是否重试',
                               result={'user_id':uid,'status':'unconfirmed'})
        return {'user_id':uid,'following':following,'raw':raw}

    @api_method
    def follow(self,username):
        uid=self.get(username)['user_id']
        return self._relationship(XWriteAPI.follow_user(self.client.auth,uid),uid,True)

    @api_method
    def unfollow(self,username):
        uid=self.get(username)['user_id']
        return self._relationship(XWriteAPI.unfollow_user(self.client.auth,uid),uid,False)


class Posts:
    def __init__(self,client):self.client=client

    @api_method
    def create(self,text, *, images=None,reply_to=None,quote_url=None,note=None):
        if not isinstance(text,str) or not text.strip():raise ValueError('text cannot be empty')
        ok,_,raw=XWriteAPI.post_tweet(self.client.auth,text,images=images,reply_to=reply_to,
                                    quote_url=quote_url,note=note)
        if isinstance(raw,dict) and raw.get('errors'):raise graphql_error(raw['errors'])
        try:
            post_id=XWriteAPI.extract_tweet_id(raw) if ok else None
        except (KeyError,TypeError,AttributeError):
            post_id=None
        if not post_id:
            raise XClientError('post_unconfirmed','发帖回执未确认，请先核对账号时间线，勿直接重发',
                               result={'status':'unconfirmed','delivery_status':'unknown'})
        return {'post_id':post_id,'url':f'https://x.com/i/status/{post_id}','status':'sent'}

    @api_method
    def thread(self,texts, *, images=None,reply_to=None,interval=2.0):
        ok,_,ids=XWriteAPI.post_thread(self.client.auth,texts,images=images,reply_to=reply_to,interval=interval)
        result={'post_ids':list(ids),'status':'sent' if ok else 'partial'}
        if not ok:
            result.update(failed_index=getattr(ids,'failed_index',len(ids)),
                          delivery_status=getattr(ids,'delivery_status','unknown'))
            raise XClientError('thread_incomplete','串文未全部发出，保留已发作品 ID，先核对当前项，勿整体重发',result=result)
        return result

    @api_method
    def get(self,post_id):
        target=parse_tweet_id(post_id)
        items=_page(XAPI.get_work_info(self.client.auth,target),'detail')['items']
        for item in items:
            if item['work_id']==target:return item
        raise XClientError('post_unavailable','作品不可访问或响应不含目标作品')

    @api_method
    def search(self,query, *, product='Latest',count=20,cursor=None):
        if product not in ('Top','Latest','Media'):
            raise ValueError('posts.search supports Top, Latest and Media')
        return _page(XAPI.search_work(self.client.auth,query,product=product,count=count,cursor=cursor),'search')

    @api_method
    def home(self, *, count=20,cursor=None):
        return _page(XAPI.get_home_timeline(self.client.auth,count=count,cursor=cursor),'home')

    @api_method
    def delete(self,post_id):return _checked(XWriteAPI.delete_tweet(self.client.auth,post_id))
    @api_method
    def like(self,post_id):return _checked(XWriteAPI.favorite_tweet(self.client.auth,post_id))
    @api_method
    def unlike(self,post_id):return _checked(XWriteAPI.unfavorite_tweet(self.client.auth,post_id))
    @api_method
    def repost(self,post_id):return _checked(XWriteAPI.create_retweet(self.client.auth,post_id))
    @api_method
    def undo_repost(self,post_id):return _checked(XWriteAPI.delete_retweet(self.client.auth,post_id))
    @api_method
    def bookmark(self,post_id):return _checked(XWriteAPI.create_bookmark(self.client.auth,post_id))
    @api_method
    def unbookmark(self,post_id):return _checked(XWriteAPI.delete_bookmark(self.client.auth,post_id))


class Comments:
    def __init__(self,client):self.client=client

    @api_method
    def list(self,post_id, *, cursor=None):
        target=parse_tweet_id(post_id)
        raw=validated_timeline(_checked(XAPI.get_work_comments(self.client.auth,target,cursor=cursor)),'detail')
        # Only direct replies, not ancestor posts or recommendations in TweetDetail.
        items=[]
        for entry in extract_tweet_entries(raw):
            tweet=entry.get('tweet',entry)
            if (tweet.get('legacy') or {}).get('in_reply_to_status_id_str')==target:
                items.append(normalized_post(tweet))
        return {'items':items,'next_cursor':extract_cursor(raw,'Bottom')}

    @api_method
    def create(self,post_id,text, *, images=None):
        return self.client.posts.create(text,reply_to=post_id,images=images)


class Media:
    def __init__(self,client):self.client=client
    @api_method
    def upload(self,path, *, category=None):
        return {'media_id':XMediaAPI.upload(self.client.auth,str(path),media_category=category)}
    @api_method
    def upload_many(self,paths):return {'media_ids':XMediaAPI.upload_many(self.client.auth,[str(p) for p in paths])}


class Articles:
    def __init__(self,client):self.client=client

    @api_method
    def create(self,markdown, *, title=None,cover=None,publish=False,caption='',reply_mode=None,base_dir=None):
        return self._write(markdown,title=title,cover=cover,publish=publish,caption=caption,reply_mode=reply_mode,base_dir=base_dir)

    @api_method
    def update(self,article_id,markdown, *, title=None,cover=None,base_dir=None):
        return self._write(markdown,article_id=article_id,title=title,cover=cover,base_dir=base_dir)

    def _write(self,markdown, *, caption='',**kwargs):
        ok,_,info=XArticleAPI.post_article(self.client.auth,markdown=markdown,tweet_text=caption,**kwargs)
        if not ok:raise XClientError('article_incomplete','文章操作未完成，请核对返回的草稿 ID',
                                   result={k:v for k,v in info.items() if k in ('article_id','edit_url','tweet_id')})
        return {k:v for k,v in info.items() if k in ('article_id','edit_url','tweet_id')}

    @api_method
    def list(self, *, lifecycle='Draft',count=20,cursor=None):
        raw=_checked(XArticleAPI.list_articles(self.client.auth,lifecycle=lifecycle,count=count,cursor=cursor))
        page=raw['data']['user']['result']['articles_article_mixer_slice']
        if not isinstance(page,dict) or not isinstance(page.get('items'),list):
            raise XClientError('invalid_response','文章响应缺少列表结构')
        items=[]
        for item in page['items']:
            article=item['article_entity_results']['result']
            items.append({'article_id':article['rest_id'],'title':article.get('title'),
                          'lifecycle':(article.get('lifecycle_state') or {}).get('lifecycle')})
        return {'items':items,'pagination':{k:v for k,v in page.items() if k not in ('items','__typename')}}
    @api_method
    def publish(self,article_id, *, caption='',reply_mode=None):
        raw=_checked(XArticleAPI.publish(self.client.auth,article_id,tweet_text=caption,reply_mode=reply_mode))
        post_id=XArticleAPI.extract_tweet_id(raw)
        if not post_id:raise XClientError('article_unconfirmed','发布回执缺少作品 ID')
        return {'article_id':article_id,'post_id':post_id,'status':'sent'}
    @api_method
    def delete(self,article_id):return _checked(XArticleAPI.delete(self.client.auth,article_id))


class XClient:
    """One authenticated account. Private state is isolated by verified user ID."""
    def __init__(self,auth, *, state_dir='.xapis',default_pin=None,existing_pin=None,identity_path=None):
        self.auth=auth
        self.state_dir=Path(state_dir)
        self.default_pin=default_pin
        self.existing_pin=existing_pin
        self.identity_path=identity_path or None
        self.messages=Messages(self)
        self.notifications=Notifications(self)
        self.lists=Lists(self)
        self.users=Users(self)
        self.posts=Posts(self)
        self.comments=Comments(self)
        self.media=Media(self)
        self.articles=Articles(self)

    @classmethod
    @api_method
    def from_cookies(cls,cookies, *, proxy=None,**options):
        auth=XAuth(proxies={'http':proxy,'https':proxy} if proxy else None).prepare_auth(cookies)
        return cls(auth,**options)

    @classmethod
    @api_method
    def login(cls,username,password, *, profile,proxy=None,on_challenge=None,**options):
        """Reuse the existing strict Python login; challenges and source profile remain required."""
        from x_apis.login_api import XJetfuelLoginApi,LoginChallenge
        if profile is None:raise ValueError('profile is required')
        api=XJetfuelLoginApi(proxies={'http':proxy,'https':proxy} if proxy else None,
                            pure_rl_values=profile,pure_required=True)
        try:auth=api.login(username,password,on_challenge=on_challenge)
        except LoginChallenge as exc:
            raise XClientError('login_challenge','登录需要用户处理挑战',
                               result={'challenge_type':exc.subtask_id}) from None
        return cls(auth,**options)

    @classmethod
    @api_method
    def from_env(cls,path='.env', **overrides):
        """Load configuration without changing os.environ; process environment wins."""
        values={**dotenv_values(path),**os.environ}
        proxy=values.get('X_PROXY')
        auth=XAuth(proxies={'http':proxy,'https':proxy} if proxy else None)
        auth.prepare_auth(values.get('X_COOKIES') or '')
        options={'state_dir':values.get('X_CHAT_STATE_DIR') or '.xapis',
                 'default_pin':values.get('X_CHAT_DEFAULT_PIN') or None,
                 'existing_pin':values.get('X_CHAT_PIN') or None,
                 'identity_path':values.get('X_CHAT_IDENTITY_FILE') or None}
        options.update(overrides)
        base=Path(path).resolve().parent
        for key in ('state_dir','identity_path'):
            value=options.get(key)
            if value and not Path(value).is_absolute():options[key]=base/Path(value)
        return cls(auth,**options)
