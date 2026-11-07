"""Offline write canaries: real journal, simulated service and no credentials."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api_watch import write_probes as probes
from api_watch.config import DEFAULTS
from api_watch.execution import ProbeContext
from api_watch.state import Store


def receipt():
    return {'data': {'mutation': {'confirmed': True}}}


class Service:
    """State is separate from receipts, so a HTTP-success-only bug is visible."""
    def __init__(self):
        self.posts, self.articles, self.media = {}, {}, {}
        self.following, self.next_id = False, 1000

    def new_id(self):
        self.next_id += 1
        return str(self.next_id)

    def create(self, auth, text, media_ids=None, reply_to=None, quote_url=None, **kwargs):
        tweet_id = self.new_id()
        legacy = {'full_text': text, 'favorited': False, 'retweeted': False, 'bookmarked': False}
        if reply_to:
            legacy['in_reply_to_status_id_str'] = reply_to
        if quote_url:
            legacy['quoted_status_id_str'] = quote_url.rsplit('/', 1)[-1]
        if media_ids:
            legacy['extended_entities'] = {'media': [
                {'id_str': media_id, 'type': 'video' if self.media[media_id]['type'].startswith('video/') else 'photo'}
                for media_id in media_ids]}
        tweet = {'__typename': 'Tweet', 'rest_id': tweet_id,
                 'core': {'user_results': {'result': {'rest_id': '101'}}}, 'legacy': legacy}
        self.posts[tweet_id] = tweet
        return {'data': {'create_tweet': {'tweet_results': {'result': copy.deepcopy(tweet)}}}}

    def result(self, auth, tweet_id):
        return {'data': {'tweetResult': {'result': copy.deepcopy(self.posts.get(tweet_id))}}}

    def delete(self, auth, tweet_id):
        self.posts.pop(tweet_id, None)
        return receipt()

    def toggle(self, field, expected):
        def mutate(auth, tweet_id):
            self.posts[tweet_id]['legacy'][field] = expected
            return receipt()
        return mutate

    def user(self, auth, username):
        return {'data': {'user': {'result': {'rest_id': '202', 'legacy': {'following': self.following}}}}}

    def follow(self, auth, peer_id):
        self.following = True
        return {'id_str': peer_id, 'following': True}

    def unfollow(self, auth, peer_id):
        self.following = False
        return {'id_str': peer_id, 'following': False}

    def media_init(self, auth, total_bytes, media_type, media_category=None):
        media_id = self.new_id()
        self.media[media_id] = {'size': total_bytes, 'type': media_type, 'payload': b'', 'category': media_category}
        return media_id

    def append(self, auth, media_id, chunk, segment_index):
        self.media[media_id]['payload'] += chunk
        return SimpleNamespace(status_code=204)

    def finalize(self, auth, media_id, original_md5=None):
        media = self.media[media_id]
        if len(media['payload']) != media['size']:
            raise ValueError('partial upload')
        return {'media_id_string': media_id, 'size': media['size'], 'processing_info': {'state': 'succeeded'}}

    def media_status(self, auth, media_id):
        return {'media_id_string': media_id, 'processing_info': {'state': 'succeeded'}}

    def article_create(self, auth, title='', **kwargs):
        article_id = self.new_id()
        article = {'rest_id': article_id, 'title': title, 'content_state': None,
                   'lifecycle_state': {'lifecycle': 'Draft'}}
        self.articles[article_id] = article
        return {'data': {'articleentity_create': {'article_entity_results': {'result': copy.deepcopy(article)}}}}

    def article_update(self, key):
        def update(auth, article_id, value):
            self.articles[article_id][key] = {'media_id': value} if key == 'cover_media' else copy.deepcopy(value)
            return receipt()
        return update

    def article_list(self, auth, lifecycle='Draft', **kwargs):
        items = [{'article_entity_results': {'result': copy.deepcopy(article)}}
                 for article in self.articles.values() if article['lifecycle_state']['lifecycle'] == lifecycle]
        return {'data': {'user': {'result': {'articles_article_mixer_slice': {'items': items}}}}}

    def publish(self, auth, article_id, tweet_text=''):
        self.articles[article_id]['lifecycle_state']['lifecycle'] = 'Published'
        raw = self.create(auth, tweet_text)
        tweet = raw['data']['create_tweet']['tweet_results']['result']
        self.articles[article_id]['metadata'] = {'tweet_results': {'result': tweet}}
        return {'data': {'articleentity_publish': {'article_entity_results': {'result': copy.deepcopy(self.articles[article_id])}}}}

    def article_delete(self, auth, article_id):
        article = self.articles.pop(article_id)
        tweet_id = article.get('metadata', {}).get('tweet_results', {}).get('result', {}).get('rest_id')
        self.posts.pop(tweet_id, None)
        return receipt()


class WriteProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state')
        self.addCleanup(self.store.close)
        self.service = Service()
        self.config = copy.deepcopy(DEFAULTS)
        self.config['writes'].update(enabled=True, allow_publish=True, allow_follow=True, premium=True, articles=True)
        self.config['samples'].update(test_account_id='101', peer_user_id='202', peer_username='watch_peer')
        for filename in ('canary.png', 'canary.mp4'):
            (self.root / filename).write_bytes(b'offline fixture')
        self.config['samples'].update(image_file=str(self.root / 'canary.png'), video_file=str(self.root / 'canary.mp4'))
        event = Mock(spec=threading.Event)
        event.is_set.return_value = False
        event.wait.return_value = False
        self.ctx = ProbeContext(self.config, {}, self.store, scenario='writes', actor_id='101',
                                auth=SimpleNamespace(user_id='101'), stop_event=event, run_id='offline-round')
        self.ctx.pacer.wait = Mock()
        self.network = self.enterContext(patch('builder.client.post', side_effect=AssertionError('network forbidden')))
        self.network_get = self.enterContext(patch('builder.client.get', side_effect=AssertionError('network forbidden')))
        self.mocks = {}
        definitions = {
            'XAPI.get_work_result': self.service.result, 'XAPI.get_user_info': self.service.user,
            'XWriteAPI.create_tweet': self.service.create, 'XWriteAPI.create_note_tweet': self.service.create,
            'XWriteAPI.delete_tweet': self.service.delete,
            'XWriteAPI.favorite_tweet': self.service.toggle('favorited', True),
            'XWriteAPI.unfavorite_tweet': self.service.toggle('favorited', False),
            'XWriteAPI.create_retweet': self.service.toggle('retweeted', True),
            'XWriteAPI.delete_retweet': self.service.toggle('retweeted', False),
            'XWriteAPI.create_bookmark': self.service.toggle('bookmarked', True),
            'XWriteAPI.delete_bookmark': self.service.toggle('bookmarked', False),
            'XWriteAPI.follow_user': self.service.follow, 'XWriteAPI.unfollow_user': self.service.unfollow,
            'XMediaAPI.init': self.service.media_init, 'XMediaAPI.append': self.service.append,
            'XMediaAPI.finalize': self.service.finalize, 'XMediaAPI.status': self.service.media_status,
            'XMediaAPI.metadata_create': lambda *args, **kwargs: SimpleNamespace(status_code=204),
            'XArticleAPI.create_draft': self.service.article_create,
            'XArticleAPI.update_title': self.service.article_update('title'),
            'XArticleAPI.update_content': self.service.article_update('content_state'),
            'XArticleAPI.update_cover': self.service.article_update('cover_media'),
            'XArticleAPI.publish': self.service.publish, 'XArticleAPI.list_articles': self.service.article_list,
            'XArticleAPI.delete': self.service.article_delete,
        }
        for name, action in definitions.items():
            self.mocks[name] = self.enterContext(patch('api_watch.write_probes.' + name, side_effect=action))

    def tearDown(self):
        self.network.assert_not_called()
        self.network_get.assert_not_called()

    def test_all_endpoint_specs_have_executed_and_semantically_validated_paths(self):
        probes.run(self.ctx)
        for spec in probes.SPECS:
            self.assertIn(spec['id'], self.ctx.results, spec['id'])
            self.assertEqual(self.ctx.results[spec['id']]['status'], 'ok', self.ctx.results[spec['id']])
        self.assertFalse(self.ctx.finish())
        self.assertFalse(self.service.posts)
        self.assertFalse(self.service.articles)
        self.assertFalse(self.service.following)
        self.mocks['XMediaAPI.status'].assert_called_once()
        self.assertEqual(self.ctx.results['write.media.image.metadata']['details']['verification_level'], 'protocol_receipt')
        self.assertEqual(self.ctx.results['write.media.image.roundtrip']['details']['verification_level'], 'readback')

    def test_disabled_or_wrong_account_never_writes(self):
        self.config['samples']['test_account_id'] = '999'
        probes.run(self.ctx)
        for mock in self.mocks.values():
            mock.assert_not_called()
        self.assertTrue(all(result['status'] == 'blocked' for result in self.ctx.results.values()))

    def test_original_follow_relationship_is_never_removed(self):
        self.service.following = True
        probes._follow(self.ctx)
        self.mocks['XWriteAPI.follow_user'].assert_not_called()
        self.mocks['XWriteAPI.unfollow_user'].assert_not_called()
        self.assertTrue(self.service.following)
        self.assertEqual(self.ctx.results['write.follow.create']['status'], 'blocked')

    def test_missing_following_flag_does_not_guess_original_state(self):
        self.mocks['XAPI.get_user_info'].side_effect = lambda *args: {'data': {'user': {'result': {'rest_id': '202'}}}}
        probes._follow(self.ctx)
        self.mocks['XWriteAPI.follow_user'].assert_not_called()
        self.mocks['XWriteAPI.unfollow_user'].assert_not_called()
        self.assertEqual(self.ctx.results['write.follow.preflight']['status'], 'incomplete')

    def test_follow_timeout_does_not_retry_and_inverse_is_journalled(self):
        def timeout(auth, peer_id):
            self.service.following = True
            raise TimeoutError('PRIVATE_TIMEOUT_TEXT')
        self.mocks['XWriteAPI.follow_user'].side_effect = timeout
        probes._follow(self.ctx)
        probes._follow(self.ctx)
        self.mocks['XWriteAPI.follow_user'].assert_called_once()
        self.mocks['XWriteAPI.unfollow_user'].assert_called_once()
        self.assertFalse(self.service.following)
        self.assertEqual(self.ctx.results['write.follow.create']['status'], 'unconfirmed')
        self.assertTrue(self.ctx.finish())
        self.assertNotIn('PRIVATE_TIMEOUT_TEXT', str(self.ctx.results))

    def test_unknown_create_result_never_retries_or_deletes_unrelated_post(self):
        self.service.posts['99'] = {'rest_id': '99'}
        self.mocks['XWriteAPI.create_tweet'].side_effect = TimeoutError('network response lost')
        probes._create_tweet(self.ctx, 'write.tweet.create', self.ctx.marker)
        probes._create_tweet(self.ctx, 'write.tweet.create', self.ctx.marker)
        probes._cleanup_tweets(self.ctx)
        self.mocks['XWriteAPI.create_tweet'].assert_called_once()
        self.mocks['XWriteAPI.delete_tweet'].assert_not_called()
        self.assertIn('99', self.service.posts)
        self.assertTrue(self.ctx.finish())

    def test_readback_failure_keeps_created_pointer_for_cleanup(self):
        self.mocks['XAPI.get_work_result'].side_effect = lambda *args: {'data': {'tweetResult': {'result': None}}}
        outcome = probes._create_tweet(self.ctx, 'write.tweet.create', self.ctx.marker)
        self.assertFalse(outcome.ok)
        resources = self.ctx.resources('tweet')
        self.assertEqual(len(resources), 1)
        self.mocks['XAPI.get_work_result'].side_effect = self.service.result
        probes._cleanup_tweets(self.ctx)
        self.assertTrue(self.ctx.resources('tweet')[0]['cleaned'])
        self.assertTrue(self.ctx.finish(), 'ambiguous earlier step must not silently become green')

    def test_cleanup_receipt_without_disappearance_is_not_green(self):
        probes._create_tweet(self.ctx, 'write.tweet.create', self.ctx.marker)
        self.mocks['XWriteAPI.delete_tweet'].side_effect = lambda *args: receipt()
        probes._cleanup_tweets(self.ctx)
        self.assertEqual(self.ctx.results['write.tweet.delete']['status'], 'cleanup_failed')
        self.assertFalse(self.ctx.resources('tweet')[0]['cleaned'])
        self.assertTrue(self.ctx.finish())

    def test_cleanup_survives_stop_signal_and_preserves_business_objects(self):
        self.service.posts['99'] = {'rest_id': '99'}
        probes._create_tweet(self.ctx, 'write.tweet.create', self.ctx.marker)
        self.ctx.stop_event.is_set.return_value = True
        probes._cleanup_tweets(self.ctx)
        self.assertEqual(set(self.service.posts), {'99'})
        self.assertTrue(self.ctx.resources('tweet')[0]['cleaned'])

    def test_changed_create_id_cannot_delete_an_old_untagged_post(self):
        raw = self.service.create(self.ctx.auth, 'an old business post')
        old_id = raw['data']['create_tweet']['tweet_results']['result']['rest_id']
        self.mocks['XWriteAPI.create_tweet'].side_effect = lambda *args, **kwargs: raw
        probes._create_tweet(self.ctx, 'write.tweet.create', self.ctx.marker)
        probes._cleanup_tweets(self.ctx)
        self.mocks['XWriteAPI.delete_tweet'].assert_not_called()
        self.assertIn(old_id, self.service.posts)
        self.assertEqual(self.ctx.results['write.tweet.delete']['status'], 'cleanup_failed')

    def test_interaction_success_response_does_not_replace_readback(self):
        self.mocks['XWriteAPI.favorite_tweet'].side_effect = lambda *args: receipt()
        probes._tweets(self.ctx)
        self.assertEqual(self.ctx.results['write.tweet.favorite']['status'], 'unconfirmed')
        self.mocks['XWriteAPI.create_retweet'].assert_not_called()
        probes._cleanup_tweets(self.ctx)
        self.assertFalse(self.service.posts)

    def test_thread_partial_write_is_recorded_before_second_send(self):
        original = self.service.create
        def partial(auth, text, **kwargs):
            if text.endswith('thread 2'):
                raise TimeoutError('second outcome unknown')
            return original(auth, text, **kwargs)
        self.mocks['XWriteAPI.create_tweet'].side_effect = partial
        probes._tweets(self.ctx)
        self.assertEqual(self.ctx.results['write.tweet.thread']['status'], 'unconfirmed')
        self.assertTrue(any(row['metadata'].get('source') == 'write.tweet.thread' for row in self.ctx.resources('tweet')))
        probes._cleanup_tweets(self.ctx)
        self.assertFalse(self.service.posts)

    def test_wrong_first_thread_receipt_never_becomes_second_reply_target(self):
        old_receipt = self.service.create(self.ctx.auth, 'existing business post')
        old_id = old_receipt['data']['create_tweet']['tweet_results']['result']['rest_id']

        def stale_first_receipt(auth, text, **kwargs):
            if text.endswith('thread 1'):
                return old_receipt
            return self.service.create(auth, text, **kwargs)

        self.mocks['XWriteAPI.create_tweet'].side_effect = stale_first_receipt
        probes._tweets(self.ctx)
        self.assertEqual(self.ctx.results['write.tweet.thread']['status'], 'unconfirmed')
        calls = self.mocks['XWriteAPI.create_tweet'].call_args_list
        self.assertTrue(any(call.args[1].endswith('thread 1') for call in calls))
        self.assertFalse(any(call.args[1].endswith('thread 2') for call in calls))
        self.assertFalse(any(call.kwargs.get('reply_to') == old_id for call in calls))
        probes._cleanup_tweets(self.ctx)
        self.assertEqual(set(self.service.posts), {old_id})
        self.assertFalse(any(call.args[1] == old_id for call in self.mocks['XWriteAPI.delete_tweet'].call_args_list))

    def test_media_receipts_do_not_prove_attachment(self):
        def no_media(auth, text, **kwargs):
            kwargs.pop('media_ids', None)
            return self.service.create(auth, text, **kwargs)
        self.mocks['XWriteAPI.create_tweet'].side_effect = no_media
        probes._media(self.ctx)
        self.assertEqual(self.ctx.results['write.media.image.roundtrip']['status'], 'unconfirmed')
        self.assertEqual(self.ctx.results['write.media.image.metadata']['details']['verification_level'], 'protocol_receipt')
        probes._cleanup_tweets(self.ctx)

    def test_finalize_processed_size_can_differ_from_uploaded_png_bytes(self):
        result = probes._media_receipt({'media_id_string': '101', 'size': 2048},
                                       '101', finalized=True, expected_size=4096)
        self.assertEqual(result['verification_level'], 'protocol_receipt')
        self.assertEqual(result['downstream_required'], 'media_roundtrip')
        for size in (0, -1, True, '2048', None):
            with self.subTest(size=size), self.assertRaises(probes._Issue):
                probes._media_receipt({'media_id_string': '101', 'size': size}, '101')
        with self.assertRaises(probes._Issue):
            probes._media_receipt({'media_id_string': 'wrong', 'size': 2048}, '101')

    def test_video_status_failure_blocks_publication(self):
        self.mocks['XMediaAPI.status'].side_effect = lambda *args: {'media_id_string': 'wrong', 'processing_info': {'state': 'succeeded'}}
        self.assertIsNone(probes._upload(self.ctx, 'video'))
        self.assertEqual(self.ctx.results['write.media.video.status']['status'], 'contract_error')
        self.mocks['XWriteAPI.create_tweet'].assert_not_called()

    def test_append_timeout_never_replays_chunk_or_finalizes(self):
        self.mocks['XMediaAPI.append'].side_effect = TimeoutError('chunk outcome unknown')
        self.assertIsNone(probes._upload(self.ctx, 'video'))
        self.assertIsNone(probes._upload(self.ctx, 'video'))
        self.mocks['XMediaAPI.init'].assert_called_once()
        self.mocks['XMediaAPI.append'].assert_called_once()
        self.mocks['XMediaAPI.finalize'].assert_not_called()
        self.assertEqual(len(self.ctx.resources('media')), 1)
        self.assertTrue(self.ctx.finish())

    def test_article_content_not_in_readback_stays_unconfirmed(self):
        self.mocks['XArticleAPI.update_content'].side_effect = lambda *args: receipt()
        probes._articles(self.ctx)
        self.assertEqual(self.ctx.results['write.article.content']['status'], 'unconfirmed')
        self.mocks['XArticleAPI.publish'].assert_not_called()
        probes._cleanup_articles(self.ctx)
        self.assertFalse(self.service.articles)

    def test_article_cleanup_checks_publication_cascade(self):
        probes._articles(self.ctx)
        self.assertTrue(self.service.posts)
        def no_cascade(auth, article_id):
            self.service.articles.pop(article_id)
            return receipt()
        self.mocks['XArticleAPI.delete'].side_effect = no_cascade
        probes._cleanup_articles(self.ctx)
        self.assertEqual(self.ctx.results['write.article.delete']['status'], 'cleanup_failed')
        self.assertFalse(self.ctx.resources('article')[0]['cleaned'])
        probes._cleanup_tweets(self.ctx)
        self.assertFalse(self.service.posts)

    def test_second_context_same_run_never_resends_existing_mutation(self):
        probes._create_tweet(self.ctx, 'write.tweet.create', self.ctx.marker)
        fresh = ProbeContext(self.config, {}, self.store, scenario='writes', actor_id='101',
                             auth=self.ctx.auth, stop_event=self.ctx.stop_event, run_id=self.ctx.run_id)
        fresh.pacer.wait = Mock()
        self.assertFalse(probes._create_tweet(fresh, 'write.tweet.create', fresh.marker).ok)
        self.mocks['XWriteAPI.create_tweet'].assert_called_once()


if __name__ == '__main__':
    unittest.main()
