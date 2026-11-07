"""Journalled canaries for the SDK's non-chat write endpoints.

Only objects created by this run are edited/deleted. The account gate lives in
the runner and is repeated here as defence in depth. A timeout is never retried.
Protocol-only media receipts are labelled as such: the separate round-trip
probe must establish that the uploaded bytes became an attached media object.
"""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from api_watch.probes import _Issue, _checked
from utils.article_util import MEDIA_CATEGORY
from x_apis.x_api import XAPI
from x_apis.x_article_api import XArticleAPI
from x_apis.x_media_api import CHUNK_SIZE, XMediaAPI, guess_media_type
from x_apis.x_write_api import XWriteAPI


def _spec(check_id, operation, method, source, *requirements):
    return dict(id=check_id, operation=operation, method=method, source=source,
                requirements=['writes_enabled', 'test_account_id', *requirements],
                risk='write', scenario='writes')


SPECS = [
    _spec('write.tweet.create', 'CreateTweet', 'POST', 'x_apis.x_write_api.XWriteAPI.create_tweet', 'allow_publish'),
    _spec('write.tweet.note', 'CreateNoteTweet', 'POST', 'x_apis.x_write_api.XWriteAPI.create_note_tweet', 'allow_publish', 'premium'),
    _spec('write.tweet.reply', 'CreateTweet', 'POST', 'x_apis.x_write_api.XWriteAPI.create_tweet', 'allow_publish'),
    _spec('write.tweet.quote', 'CreateTweet', 'POST', 'x_apis.x_write_api.XWriteAPI.create_tweet', 'allow_publish'),
    _spec('write.tweet.thread', 'CreateTweet', 'POST', 'x_apis.x_write_api.XWriteAPI.create_tweet', 'allow_publish'),
    _spec('write.tweet.post', 'CreateTweet', 'POST', 'x_apis.x_write_api.XWriteAPI.post_tweet', 'allow_publish'),
    *[_spec('write.tweet.' + name, operation, 'POST', 'x_apis.x_write_api.XWriteAPI.' + source, 'allow_publish')
      for name, operation, source in (
          ('favorite', 'FavoriteTweet', 'favorite_tweet'), ('unfavorite', 'UnfavoriteTweet', 'unfavorite_tweet'),
          ('retweet', 'CreateRetweet', 'create_retweet'), ('unretweet', 'DeleteRetweet', 'delete_retweet'),
          ('bookmark', 'CreateBookmark', 'create_bookmark'), ('unbookmark', 'DeleteBookmark', 'delete_bookmark'),
          ('delete', 'DeleteTweet', 'delete_tweet'))],
    _spec('write.follow.create', '/friendships/create.json', 'POST', 'x_apis.x_write_api.XWriteAPI.follow_user', 'allow_follow', 'peer_user_id', 'peer_username'),
    _spec('write.follow.delete', '/friendships/destroy.json', 'POST', 'x_apis.x_write_api.XWriteAPI.unfollow_user', 'allow_follow', 'peer_user_id', 'peer_username'),
]
for _kind in ('image', 'video'):
    for _name, _operation, _method in (
            ('init', 'media.upload.INIT', 'POST'), ('append', 'media.upload.APPEND', 'POST'),
            ('finalize', 'media.upload.FINALIZE', 'POST'), ('metadata', 'media.metadata.create', 'POST')):
        SPECS.append(_spec('write.media.' + _kind + '.' + _name, _operation, _method,
                           'x_apis.x_media_api.XMediaAPI.' + ('metadata_create' if _name == 'metadata' else _name),
                           'allow_publish', _kind + '_file'))
    if _kind == 'video':
        SPECS.append(_spec('write.media.video.status', 'media.upload.STATUS', 'GET',
                           'x_apis.x_media_api.XMediaAPI.status', 'allow_publish', 'video_file'))
    SPECS.append(_spec('write.media.' + _kind + '.roundtrip', 'CreateTweet', 'POST',
                       'x_apis.x_write_api.XWriteAPI.create_tweet', 'allow_publish', _kind + '_file'))
for _name, _operation, _source in (
        ('create', 'ArticleEntityDraftCreate', 'create_draft'),
        ('title', 'ArticleEntityUpdateTitle', 'update_title'),
        ('content', 'ArticleEntityUpdateContent', 'update_content'),
        ('cover', 'ArticleEntityUpdateCoverMedia', 'update_cover'),
        ('publish', 'ArticleEntityPublish', 'publish'),
        ('delete', 'ArticleEntityDelete', 'delete')):
    _requirements = ['articles']
    if _name == 'cover':
        _requirements.append('image_file')
    if _name == 'publish':
        _requirements.append('allow_publish')
    SPECS.append(_spec('write.article.' + _name, _operation, 'POST',
                       'x_apis.x_article_api.XArticleAPI.' + _source, *_requirements))


def _writes(ctx):
    return ctx.config.get('writes', {})


def _stop(ctx):
    return ctx.stop_event or getattr(getattr(ctx, 'pacer', None), 'stop_event', None) or threading.Event()


def _path(raw, path):
    """Optional-field lookup; each caller validates the business requirement."""
    for key in path.split('.'):
        if not isinstance(raw, dict) or key not in raw:
            return None
        raw = raw[key]
    return raw


def _owned(ctx, kind, resource_id):
    return any(str(row['id']) == str(resource_id) and not row.get('cleaned', False)
               for row in ctx.resources(kind))


def _require_owned(ctx, kind, resource_id):
    if not _owned(ctx, kind, resource_id):
        raise _Issue('blocked', reason='resource_not_created_by_this_run')


def _id(value, field='rest_id'):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise _Issue('contract_error', fields=(field,))
    return value


def _tweet(raw, tweet_id):
    _checked(raw)
    result = _path(raw, 'data.tweetResult.result')
    if not isinstance(result, dict):
        raise _Issue('incomplete', fields=('data.tweetResult.result',))
    if result.get('__typename') == 'TweetWithVisibilityResults':
        result = result.get('tweet')
    if not isinstance(result, dict) or str(result.get('rest_id')) != str(tweet_id):
        raise _Issue('contract_error', fields=('tweet.rest_id',))
    return result


def _read_tweet(ctx, tweet_id, *, text=None, reply_to=None, quote_id=None, media_id=None, media_type=None):
    tweet = _tweet(XAPI.get_work_result(ctx.auth, tweet_id), tweet_id)
    author = _path(tweet, 'core.user_results.result.rest_id') or _path(tweet, 'legacy.user_id_str')
    if str(author) != str(ctx.actor_id):
        raise _Issue('contract_error', fields=('tweet.author_id',))
    body = _path(tweet, 'note_tweet.note_tweet_results.result.text') or _path(tweet, 'legacy.full_text')
    if not isinstance(body, str) or (text is not None and not body.startswith(text)):
        raise _Issue('contract_error', fields=('tweet.text',))
    if reply_to is not None and str(_path(tweet, 'legacy.in_reply_to_status_id_str')) != str(reply_to):
        raise _Issue('contract_error', fields=('tweet.in_reply_to_status_id_str',))
    if quote_id is not None and str(_path(tweet, 'legacy.quoted_status_id_str')) != str(quote_id):
        raise _Issue('contract_error', fields=('tweet.quoted_status_id_str',))
    if media_id is not None:
        media = _path(tweet, 'legacy.extended_entities.media')
        if not isinstance(media, list):
            raise _Issue('contract_error', fields=('tweet.extended_entities.media',))
        found = [row for row in media if isinstance(row, dict) and str(row.get('id_str')) == str(media_id)]
        if len(found) != 1 or found[0].get('type') != media_type:
            raise _Issue('contract_error', fields=('tweet.media.id_str', 'tweet.media.type'))
    return tweet


def _create_tweet(ctx, check_id, text, *, note=False, reply_to=None, quote_id=None, media_id=None, media_type=None, wrapper=False):
    def action():
        kwargs = {}
        if reply_to:
            _require_owned(ctx, 'tweet', reply_to)
            kwargs['reply_to'] = reply_to
        if quote_id:
            _require_owned(ctx, 'tweet', quote_id)
            kwargs['quote_url'] = 'https://x.com/i/status/' + quote_id
        if media_id:
            _require_owned(ctx, 'media', media_id)
            kwargs['media_ids'] = [media_id]
        if wrapper:
            ok, _, raw = XWriteAPI.post_tweet(ctx.auth, text, **kwargs)
            if not ok:
                raise _Issue('incomplete', reason='write_receipt_unconfirmed', outcome_unknown=True)
        else:
            method = XWriteAPI.create_note_tweet if note else XWriteAPI.create_tweet
            raw = method(ctx.auth, text, **kwargs)
        # Save before validation/read-back; a malformed read must not orphan it.
        tweet_id = _id(str(XWriteAPI.extract_tweet_id(raw)))
        ctx.save_resource('tweet', tweet_id, marker=ctx.marker, source=check_id)
        return tweet_id

    def validate(tweet_id):
        _read_tweet(ctx, tweet_id, text=text, reply_to=reply_to, quote_id=quote_id,
                    media_id=media_id, media_type=media_type)
        return {'verification_level': 'readback', 'owned_resource': True,
                'media_binding_verified': media_id is not None}

    return ctx.step(check_id, action, validate, mutating=True)


def _deleted_tweet(ctx, tweet_id):
    raw = XAPI.get_work_result(ctx.auth, tweet_id)
    _checked(raw)
    container = _path(raw, 'data.tweetResult')
    if not isinstance(container, dict) or 'result' not in container:
        raise _Issue('incomplete', fields=('data.tweetResult.result',))
    result = container['result']
    # Only an explicit null/deleted tombstone establishes disappearance. A
    # changed response shape, HTTP 403/404 or generic unavailable does not.
    if result is None or (isinstance(result, dict) and result.get('__typename') in ('TweetTombstone', 'TweetUnavailable')
                          and result.get('reason') in ('Deleted', 'NotFound')):
        return {'verification_level': 'readback', 'absent': True}
    raise _Issue('incomplete', reason='delete_not_confirmed')


def _cleanup_tweets(ctx):
    first = True
    for row in reversed(ctx.resources('tweet')):
        if row.get('cleaned'):
            continue
        tweet_id = str(row['id'])

        def action(tweet_id=tweet_id):
            _require_owned(ctx, 'tweet', tweet_id)
            # A changed create response must not make an unrelated old object
            # deletable merely by returning its ID. Confirm author and run tag.
            _read_tweet(ctx, tweet_id, text=ctx.marker)
            return XWriteAPI.delete_tweet(ctx.auth, tweet_id)

        def validate(raw, tweet_id=tweet_id):
            _checked(raw)
            proof = _deleted_tweet(ctx, tweet_id)
            ctx.mark_cleaned('tweet', tweet_id)
            return proof

        # The first deletion uses the catalogue ID; every further object has
        # its own durable operation key (no accidental resend within a run).
        check_id = 'write.tweet.delete' if first else 'write.tweet.delete.' + tweet_id
        first = False
        ctx.step(check_id, action, validate, mutating=True, cleanup=True)


def _tweets(ctx):
    root = _create_tweet(ctx, 'write.tweet.create', ctx.marker + ' disposable API canary')
    if not root.ok:
        return
    target = root.value
    _create_tweet(ctx, 'write.tweet.reply', ctx.marker + ' reply', reply_to=target)
    _create_tweet(ctx, 'write.tweet.quote', ctx.marker + ' quote', quote_id=target)
    _create_tweet(ctx, 'write.tweet.post', ctx.marker + ' post wrapper', wrapper=True)
    if _writes(ctx).get('premium'):
        _create_tweet(ctx, 'write.tweet.note', ctx.marker + ' long-form canary. ' + 'verification ' * 30, note=True)
    else:
        ctx.block(['write.tweet.note'], 'premium_not_enabled', status='disabled')

    texts = [ctx.marker + ' thread 1', ctx.marker + ' thread 2']

    def thread_action():
        # Build the wire-level thread explicitly: the convenience post_thread
        # helper only returns IDs at the end, which can orphan the first post
        # when a later extraction raises. Persist each receipt immediately.
        ids, parent = [], target
        for index, text in enumerate(texts):
            gap = max(1, float(ctx.config.get('probes', {}).get('request_gap_seconds', 1)))
            if index and _stop(ctx).wait(gap):
                raise _Issue('incomplete', reason='thread_interrupted', outcome_unknown=True)
            raw = XWriteAPI.create_tweet(ctx.auth, text, reply_to=parent)
            tweet_id = _id(str(XWriteAPI.extract_tweet_id(raw)))
            ctx.save_resource('tweet', tweet_id, marker=ctx.marker, source='write.tweet.thread')
            # Verify the receipt before it becomes the next mutation target.
            # A stale/wrong ID must never cause a reply to an old business post.
            _read_tweet(ctx, tweet_id, text=text, reply_to=parent)
            ids.append(tweet_id)
            parent = tweet_id
        return ids

    def thread_validate(ids):
        parent = target
        for tweet_id, text in zip(ids, texts):
            _read_tweet(ctx, _id(tweet_id), text=text, reply_to=parent)
            parent = tweet_id
        return {'verification_level': 'readback', 'item_count': len(ids)}

    ctx.step('write.tweet.thread', thread_action, thread_validate, mutating=True)
    for name, undo, create, remove, field in (
            ('favorite', 'unfavorite', XWriteAPI.favorite_tweet, XWriteAPI.unfavorite_tweet, 'favorited'),
            ('retweet', 'unretweet', XWriteAPI.create_retweet, XWriteAPI.delete_retweet, 'retweeted'),
            ('bookmark', 'unbookmark', XWriteAPI.create_bookmark, XWriteAPI.delete_bookmark, 'bookmarked')):
        def mutate(method=create):
            _require_owned(ctx, 'tweet', target)
            return method(ctx.auth, target)

        def validate(raw, expected=True, field=field):
            _checked(raw)
            result = _read_tweet(ctx, target, text=ctx.marker)
            flag = _path(result, 'legacy.' + field)
            if flag is None:
                flag = result.get(field)
            if flag is not expected:
                raise _Issue('incomplete' if flag is None else 'contract_error', fields=('tweet.' + field,))
            return {'verification_level': 'readback', 'expected_state': expected}

        outcome = ctx.step('write.tweet.' + name, mutate, validate, mutating=True)
        if outcome.ok:
            ctx.step('write.tweet.' + undo, lambda method=remove: method(ctx.auth, target),
                     lambda raw, validate=validate: validate(raw, False), mutating=True, cleanup=True)


def _media_receipt(raw, media_id, *, finalized=False, expected_size=None):
    if not isinstance(raw, dict) or str(raw.get('media_id_string') or raw.get('media_id')) != str(media_id):
        raise _Issue('contract_error', fields=('media.media_id',))
    info = raw.get('processing_info')
    if info is not None:
        if not isinstance(info, dict) or info.get('state') not in ('pending', 'in_progress', 'succeeded', 'failed'):
            raise _Issue('contract_error', fields=('media.processing_info.state',))
        if info['state'] == 'failed':
            raise _Issue('contract_error', reason='media_processing_failed')
    # FINALIZE reports the processed media size. X can recompress a PNG, so
    # equality with the uploaded byte count is not a wire-contract guarantee.
    # The later publication readback must still verify the media ID and type.
    if 'size' in raw and (type(raw['size']) is not int or raw['size'] <= 0):
        raise _Issue('contract_error', fields=('media.size',))
    return {'verification_level': 'protocol_receipt', 'identity_verified': True,
            'downstream_required': 'media_roundtrip', 'finalized': finalized}


def _empty_receipt(raw):
    if not hasattr(raw, 'status_code') or not 200 <= raw.status_code < 300:
        raise _Issue('contract_error', fields=('http_status',))
    return {'verification_level': 'protocol_receipt', 'downstream_required': 'media_roundtrip',
            'metadata_policy_readback': False}


def _upload(ctx, kind, *, article=False):
    file_path = ctx.samples.get(kind + '_file')
    prefix = 'write.article.media' if article else 'write.media.' + kind
    if not file_path:
        ctx.block([spec['id'] for spec in SPECS if spec['id'].startswith(prefix)], 'missing_' + kind + '_file', status='disabled')
        return None
    path = Path(file_path)
    # Read only explicitly configured fixtures and bound memory/upload cost.
    limit = 20 * 1024 * 1024 if kind == 'video' else 5 * 1024 * 1024
    extensions = ('.mp4', '.mov') if kind == 'video' else ('.jpg', '.jpeg', '.png', '.webp')
    if path.suffix.lower() not in extensions or not path.is_file() or not 0 < path.stat().st_size <= limit:
        ctx.block([spec['id'] for spec in SPECS if spec['id'].startswith(prefix)], 'invalid_or_oversized_test_media')
        return None
    media_type = guess_media_type(str(path))
    if (kind == 'video' and not media_type.startswith('video/')) or (kind == 'image' and media_type not in ('image/jpeg', 'image/png', 'image/webp')):
        ctx.block([spec['id'] for spec in SPECS if spec['id'].startswith(prefix)], 'unsupported_test_media_type')
        return None
    with path.open('rb') as stream:
        payload = stream.read(limit + 1)
    if not 0 < len(payload) <= limit:
        ctx.block([spec['id'] for spec in SPECS if spec['id'].startswith(prefix)], 'invalid_or_oversized_test_media')
        return None

    def init():
        media_id = _id(XMediaAPI.init(ctx.auth, len(payload), media_type,
                                    media_category=MEDIA_CATEGORY if article else None), 'media_id')
        ctx.save_resource('media', media_id, marker=ctx.marker, source=prefix,
                          cleanup_policy='server_expiration_no_delete_endpoint')
        return media_id

    outcome = ctx.step(prefix + '.init', init,
                       lambda value: {'verification_level': 'protocol_receipt', 'identity_verified': bool(_id(value)),
                                      'downstream_required': 'article_cover' if article else 'media_roundtrip'}, mutating=True)
    if not outcome.ok:
        return None
    media_id = outcome.value
    def append():
        count = 0
        for index, offset in enumerate(range(0, len(payload), CHUNK_SIZE)):
            if _stop(ctx).is_set():
                raise _Issue('incomplete', reason='media_upload_interrupted')
            _empty_receipt(XMediaAPI.append(ctx.auth, media_id, payload[offset:offset + CHUNK_SIZE], index))
            count += 1
        return count
    appended = ctx.step(prefix + '.append', append,
                        lambda count: {'verification_level': 'protocol_receipt', 'segment_count': count,
                                       'downstream_required': 'article_cover' if article else 'media_roundtrip'}, mutating=True)
    if not appended.ok:
        return None
    finalized = ctx.step(prefix + '.finalize', lambda: XMediaAPI.finalize(
        ctx.auth, media_id, original_md5=hashlib.md5(payload).hexdigest()),
        lambda raw: _media_receipt(raw, media_id, finalized=True, expected_size=len(payload)), mutating=True)
    if not finalized.ok:
        return None
    if kind == 'video':
        # STATUS is called even when FINALIZE immediately says succeeded. It
        # is a separately monitored endpoint, not optional implied coverage.
        def processing():
            deadline = time.monotonic() + 180
            raw = XMediaAPI.status(ctx.auth, media_id)
            _media_receipt(raw, media_id)
            while (raw.get('processing_info') or {}).get('state') in ('pending', 'in_progress'):
                delay = max(1, min(30, (raw['processing_info'].get('check_after_secs') or 1)))
                if time.monotonic() + delay > deadline or _stop(ctx).wait(delay):
                    raise _Issue('incomplete', reason='media_processing_not_finished')
                raw = XMediaAPI.status(ctx.auth, media_id)
                _media_receipt(raw, media_id)
            if (raw.get('processing_info') or {}).get('state') != 'succeeded':
                raise _Issue('incomplete', fields=('media.processing_info.state',))
            return raw
        status = ctx.step(prefix + '.status', processing, lambda raw: _media_receipt(raw, media_id))
        if not status.ok:
            return None
    if not article:
        metadata = ctx.step(prefix + '.metadata', lambda: XMediaAPI.metadata_create(ctx.auth, media_id),
                            _empty_receipt, mutating=True)
        if not metadata.ok:
            return None
    return media_id


def _media(ctx):
    for kind in ('image', 'video'):
        media_id = _upload(ctx, kind)
        if media_id:
            _create_tweet(ctx, 'write.media.' + kind + '.roundtrip', ctx.marker + ' ' + kind,
                          media_id=media_id, media_type='video' if kind == 'video' else 'photo')


def _article_page(ctx, lifecycle):
    raw = XArticleAPI.list_articles(ctx.auth, lifecycle=lifecycle, count=100, user_id=ctx.actor_id)
    _checked(raw)
    page = _path(raw, 'data.user.result.articles_article_mixer_slice')
    if not isinstance(page, dict) or not isinstance(page.get('items'), list):
        raise _Issue('contract_error', fields=('articles_article_mixer_slice.items',))
    rows = []
    for item in page['items']:
        row = _path(item, 'article_entity_results.result')
        if not isinstance(row, dict) or not row.get('rest_id'):
            raise _Issue('contract_error', fields=('article.rest_id',))
        rows.append(row)
    return rows, page


def _article(ctx, article_id, lifecycle='Draft'):
    rows, _ = _article_page(ctx, lifecycle)
    matches = [row for row in rows if str(row['rest_id']) == str(article_id)]
    if len(matches) != 1:
        raise _Issue('incomplete', reason='created_article_not_in_readback_page')
    article = matches[0]
    if _path(article, 'lifecycle_state.lifecycle') != lifecycle:
        raise _Issue('contract_error', fields=('article.lifecycle_state.lifecycle',))
    return article


def _articles(ctx):
    def create():
        raw = XArticleAPI.create_draft(ctx.auth, title=ctx.marker)
        article_id = _id(str(XArticleAPI.extract_article_id(raw)), 'article_id')
        ctx.save_resource('article', article_id, marker=ctx.marker)
        return article_id

    def created(article_id):
        if _article(ctx, article_id).get('title') != ctx.marker:
            raise _Issue('contract_error', fields=('article.title',))
        return {'verification_level': 'readback', 'lifecycle': 'Draft'}

    outcome = ctx.step('write.article.create', create, created, mutating=True)
    if not outcome.ok:
        return
    article_id = outcome.value
    title = ctx.marker + ' article'
    content = {'blocks': [{'data': {}, 'text': ctx.marker + ' disposable article body', 'key': 'watch',
                           'type': 'unstyled', 'entity_ranges': [], 'inline_style_ranges': []}], 'entity_map': []}

    def changed(raw, key, expected):
        _checked(raw)
        value = _article(ctx, article_id).get(key)
        if value != expected:
            raise _Issue('incomplete' if value is None else 'contract_error', fields=('article.' + key,))
        return {'verification_level': 'readback'}

    titled = ctx.step('write.article.title', lambda: XArticleAPI.update_title(ctx.auth, article_id, title),
                     lambda raw: changed(raw, 'title', title), mutating=True)
    if not titled.ok:
        return
    filled = ctx.step('write.article.content', lambda: XArticleAPI.update_content(ctx.auth, article_id, content),
                     lambda raw: changed(raw, 'content_state', content), mutating=True)
    if not filled.ok:
        return
    cover = _upload(ctx, 'image', article=True)
    if cover:
        def cover_validate(raw):
            _checked(raw)
            value = _article(ctx, article_id).get('cover_media')
            if not isinstance(value, dict) or str(value.get('media_id') or value.get('media_id_str') or value.get('id_str')) != cover:
                raise _Issue('incomplete', fields=('article.cover_media.media_id',))
            return {'verification_level': 'readback', 'media_binding_verified': True}
        covered = ctx.step('write.article.cover', lambda: XArticleAPI.update_cover(ctx.auth, article_id, cover),
                           cover_validate, mutating=True)
        if not covered.ok:
            return
    else:
        ctx.block(['write.article.cover'], 'missing_or_unavailable_test_cover', status='disabled')
    if _writes(ctx).get('allow_publish'):
        def publish():
            raw = XArticleAPI.publish(ctx.auth, article_id, tweet_text=ctx.marker + ' article publication')
            tweet_id = _id(str(XArticleAPI.extract_tweet_id(raw)))
            ctx.save_resource('tweet', tweet_id, marker=ctx.marker, article_id=article_id)
            return tweet_id

        def published(tweet_id):
            _article(ctx, article_id, 'Published')
            _read_tweet(ctx, tweet_id, text=ctx.marker)
            return {'verification_level': 'readback', 'lifecycle': 'Published', 'post_verified': True}
        ctx.step('write.article.publish', publish, published, mutating=True)
    else:
        ctx.block(['write.article.publish'], 'allow_publish_not_enabled', status='disabled')


def _cleanup_articles(ctx):
    for row in ctx.resources('article'):
        if row.get('cleaned'):
            continue
        article_id = str(row['id'])

        def action(article_id=article_id):
            _require_owned(ctx, 'article', article_id)
            owned = False
            for lifecycle in ('Draft', 'Published'):
                articles, _ = _article_page(ctx, lifecycle)
                for article in articles:
                    if str(article['rest_id']) == article_id:
                        title = article.get('title')
                        if not isinstance(title, str) or not title.startswith(ctx.marker):
                            raise _Issue('blocked', reason='article_run_marker_not_verified')
                        owned = True
            if not owned:
                raise _Issue('incomplete', reason='article_ownership_not_visible_in_readback')
            return XArticleAPI.delete(ctx.auth, article_id)

        def validate(raw, article_id=article_id):
            _checked(raw)
            for lifecycle in ('Draft', 'Published'):
                articles, page = _article_page(ctx, lifecycle)
                if any(str(article['rest_id']) == article_id for article in articles):
                    raise _Issue('incomplete', reason='article_delete_not_confirmed')
                if len(articles) >= 100 or any(page.get(key) for key in ('next_cursor', 'nextCursor', 'cursor')):
                    raise _Issue('incomplete', reason='article_absence_not_proven_beyond_first_page')
            # Publishing may create a tweet. Check its absence independently
            # before marking the deletion cascade confirmed.
            for tweet in ctx.resources('tweet'):
                if tweet.get('metadata', {}).get('article_id') == article_id and not tweet.get('cleaned'):
                    _deleted_tweet(ctx, tweet['id'])
                    ctx.mark_cleaned('tweet', tweet['id'])
            ctx.mark_cleaned('article', article_id)
            return {'verification_level': 'readback', 'absent': True}

        ctx.step('write.article.delete', action, validate, mutating=True, cleanup=True)


def _following(ctx, peer_id, username):
    raw = XAPI.get_user_info(ctx.auth, username)
    _checked(raw)
    user = _path(raw, 'data.user.result')
    if not isinstance(user, dict) or str(user.get('rest_id')) != peer_id:
        raise _Issue('contract_error', fields=('user.rest_id',))
    following = _path(user, 'relationship_perspectives.following')
    if following is None:
        following = _path(user, 'legacy.following')
    if type(following) is not bool:
        raise _Issue('incomplete', fields=('user.following',))
    return following


def _follow(ctx):
    ids = ['write.follow.create', 'write.follow.delete']
    peer_id, username = str(ctx.samples.get('peer_user_id') or ''), ctx.samples.get('peer_username')
    if not _writes(ctx).get('allow_follow') or not peer_id or not username or peer_id == str(ctx.actor_id):
        ctx.block(ids, 'explicit_test_peer_and_allow_follow_required', status='disabled')
        return
    baseline = ctx.step('write.follow.preflight', lambda: _following(ctx, peer_id, username),
                        lambda value: {'following': value})
    if not baseline.ok:
        ctx.block(ids, 'peer_relationship_preflight_failed')
        return
    if baseline.value:
        ctx.block(ids, 'test_peer_already_followed_preserve_original_relationship')
        return

    def follow():
        # Recording the known target before the call also covers a response
        # timeout. Cleanup is an inverse operation on an originally absent edge.
        ctx.save_resource('follow', peer_id, peer_username=username, original_following=False)
        return XWriteAPI.follow_user(ctx.auth, peer_id)

    def verify(raw, expected):
        if not isinstance(raw, dict) or raw.get('errors') or str(raw.get('id_str') or raw.get('id')) != peer_id:
            raise _Issue('contract_error', fields=('friendship.user_id',))
        if _following(ctx, peer_id, username) is not expected:
            raise _Issue('incomplete', reason='following_readback_mismatch')
        if not expected:
            ctx.mark_cleaned('follow', peer_id)
        return {'verification_level': 'readback', 'expected_state': expected}

    try:
        ctx.step('write.follow.create', follow, lambda raw: verify(raw, True), mutating=True)
    finally:
        if _owned(ctx, 'follow', peer_id):
            # No repeat of the original follow after an ambiguous outcome.
            ctx.step('write.follow.delete', lambda: XWriteAPI.unfollow_user(ctx.auth, peer_id),
                     lambda raw: verify(raw, False), mutating=True, cleanup=True)


def run(ctx):
    """Run allowed write scenarios; never bootstrap account/login credentials."""
    all_ids = [spec['id'] for spec in SPECS]
    if not _writes(ctx).get('enabled'):
        ctx.block(all_ids, 'writes_not_enabled', status='disabled')
        return
    if not str(ctx.actor_id or '').isdigit() or str(ctx.samples.get('test_account_id') or '') != str(ctx.actor_id):
        ctx.block(all_ids, 'viewer_does_not_match_explicit_test_account')
        return
    try:
        if _writes(ctx).get('allow_publish'):
            _tweets(ctx)
            _media(ctx)
        else:
            ctx.block([spec['id'] for spec in SPECS if spec['id'].startswith(('write.tweet.', 'write.media.'))],
                      'allow_publish_not_enabled', status='disabled')
        if _writes(ctx).get('articles'):
            _articles(ctx)
        else:
            ctx.block([spec['id'] for spec in SPECS if spec['id'].startswith('write.article.')],
                      'articles_not_enabled', status='disabled')
        _follow(ctx)
    finally:
        _cleanup_articles(ctx)
        _cleanup_tweets(ctx)
