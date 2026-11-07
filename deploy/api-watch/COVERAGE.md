# XApis 探针覆盖清单

共 91 个探针场景/变体。这里列实现清单；真实执行结果请运行 `coverage`，未配置、未执行、被阻断都不算通过。

| ID | 操作 | 方法 | 前置参数 |
|---|---|---|---|
| guest.activate | GuestActivate | POST |  |
| guest.user | UserByScreenName | GET | samples.username |
| guest.timeline | UserTweets | GET | samples.username |
| guest.timeline.page2 | UserTweets | GET | samples.username, probes.pagination |
| auth.viewer | Viewer | GET | cookies |
| read.user.profile | UserByScreenName | GET | cookies, samples.username |
| read.user.originals | UserOriginalsTimeline | GET | cookies, samples.username |
| read.user.originals.page2 | UserOriginalsTimeline | GET | cookies, samples.username |
| read.user.tweets | UserTweets | GET | cookies, samples.username |
| read.user.tweets.page2 | UserTweets | GET | cookies, samples.username |
| read.home.get | HomeTimeline | GET | cookies |
| read.home.get.page2 | HomeTimeline | GET | cookies |
| read.home.post | HomeTimeline | POST | cookies, samples.post_id / home.get.item |
| read.home.post.page2 | HomeTimeline | POST | cookies, samples.post_id / home.get.item |
| read.search.top | SearchTimeline | POST | cookies, samples.search_query |
| read.search.top.page2 | SearchTimeline | POST | cookies, samples.search_query |
| read.search.latest | SearchTimeline | POST | cookies, samples.search_query |
| read.search.latest.page2 | SearchTimeline | POST | cookies, samples.search_query |
| read.search.people | SearchTimeline | POST | cookies, samples.search_query |
| read.search.people.page2 | SearchTimeline | POST | cookies, samples.search_query |
| read.search.media | SearchTimeline | POST | cookies, samples.search_query |
| read.search.media.page2 | SearchTimeline | POST | cookies, samples.search_query |
| read.search.lists | ListSearchTimeline | GET | cookies, samples.search_query |
| read.search.lists.page2 | ListSearchTimeline | GET | cookies, samples.search_query |
| read.notifications.all | NotificationsTimeline | GET | cookies |
| read.notifications.all.page2 | NotificationsTimeline | GET | cookies |
| read.notifications.mentions | NotificationsTimeline | GET | cookies |
| read.notifications.mentions.page2 | NotificationsTimeline | GET | cookies |
| read.notifications.verified | NotificationsTimeline | GET | cookies |
| read.notifications.verified.page2 | NotificationsTimeline | GET | cookies |
| read.notifications.priority | NotificationsTimeline | GET | cookies |
| read.notifications.priority.page2 | NotificationsTimeline | GET | cookies |
| read.notifications.superfollowers | NotificationsTimeline | GET | cookies |
| read.notifications.superfollowers.page2 | NotificationsTimeline | GET | cookies |
| read.post.detail | TweetDetail | GET | cookies, samples.post_id |
| read.post.detail.page2 | TweetDetail | GET | cookies, samples.post_id |
| read.post.hydration | TweetResultByRestId | GET | cookies, samples.post_id |
| read.post.comments | TweetDetail | GET | cookies, samples.post_id |
| read.post.comments.page2 | TweetDetail | GET | cookies, samples.post_id |
| read.articles.draft | ArticleEntitiesSlice | GET | cookies |
| read.articles.draft.page2 | ArticleEntitiesSlice | GET | cookies |
| read.articles.published | ArticleEntitiesSlice | GET | cookies |
| read.articles.published.page2 | ArticleEntitiesSlice | GET | cookies |
| read.chat.settings | xChatDmSettingsQuery | GET | cookies, samples.chat_settings_contract_file |
| read.chat.members | GetUsersByIdsForXChat | GET | cookies, samples.peer_user_id |
| read.chat.public_keys | GetPublicKeysQuery | GET | cookies, samples.peer_user_id |
| read.chat.inbox | GetInitialXChatPageQuery | GET | cookies |
| read.chat.conversation | GetConversationPageQuery | GET | cookies, samples.conversation_id |
| read.chat.conversation.page2 | GetConversationPageQuery | GET | cookies, samples.conversation_id |
| write.tweet.create | CreateTweet | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.note | CreateNoteTweet | POST | writes_enabled, test_account_id, allow_publish, premium |
| write.tweet.reply | CreateTweet | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.quote | CreateTweet | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.thread | CreateTweet | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.post | CreateTweet | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.favorite | FavoriteTweet | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.unfavorite | UnfavoriteTweet | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.retweet | CreateRetweet | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.unretweet | DeleteRetweet | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.bookmark | CreateBookmark | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.unbookmark | DeleteBookmark | POST | writes_enabled, test_account_id, allow_publish |
| write.tweet.delete | DeleteTweet | POST | writes_enabled, test_account_id, allow_publish |
| write.follow.create | /friendships/create.json | POST | writes_enabled, test_account_id, allow_follow, peer_user_id, peer_username |
| write.follow.delete | /friendships/destroy.json | POST | writes_enabled, test_account_id, allow_follow, peer_user_id, peer_username |
| write.media.image.init | media.upload.INIT | POST | writes_enabled, test_account_id, allow_publish, image_file |
| write.media.image.append | media.upload.APPEND | POST | writes_enabled, test_account_id, allow_publish, image_file |
| write.media.image.finalize | media.upload.FINALIZE | POST | writes_enabled, test_account_id, allow_publish, image_file |
| write.media.image.metadata | media.metadata.create | POST | writes_enabled, test_account_id, allow_publish, image_file |
| write.media.image.roundtrip | CreateTweet | POST | writes_enabled, test_account_id, allow_publish, image_file |
| write.media.video.init | media.upload.INIT | POST | writes_enabled, test_account_id, allow_publish, video_file |
| write.media.video.append | media.upload.APPEND | POST | writes_enabled, test_account_id, allow_publish, video_file |
| write.media.video.finalize | media.upload.FINALIZE | POST | writes_enabled, test_account_id, allow_publish, video_file |
| write.media.video.metadata | media.metadata.create | POST | writes_enabled, test_account_id, allow_publish, video_file |
| write.media.video.status | media.upload.STATUS | GET | writes_enabled, test_account_id, allow_publish, video_file |
| write.media.video.roundtrip | CreateTweet | POST | writes_enabled, test_account_id, allow_publish, video_file |
| write.article.create | ArticleEntityDraftCreate | POST | writes_enabled, test_account_id, articles |
| write.article.title | ArticleEntityUpdateTitle | POST | writes_enabled, test_account_id, articles |
| write.article.content | ArticleEntityUpdateContent | POST | writes_enabled, test_account_id, articles |
| write.article.cover | ArticleEntityUpdateCoverMedia | POST | writes_enabled, test_account_id, articles, image_file |
| write.article.publish | ArticleEntityPublish | POST | writes_enabled, test_account_id, articles, allow_publish |
| write.article.delete | ArticleEntityDelete | POST | writes_enabled, test_account_id, articles |
| lifecycle.login.jetfuel | JetfuelLogin | POST | lifecycle.login_enabled, secrets.login, samples.test_account_id |
| lifecycle.login.legacy | LegacyOnboardingLogin | POST | lifecycle.legacy_login_enabled, secrets.login, samples.test_account_id |
| lifecycle.chat.token | GenerateXChatTokenMutation | POST | lifecycle.chat_token, cookies, samples.test_account_id |
| lifecycle.chat.identity_register | AddXChatPublicKeyMutation | POST | lifecycle.chat_identity, secrets.chat_pin, unregistered_test_account |
| lifecycle.chat.identity_backup | ChatSDKSetupAndUnlock | POST | lifecycle.chat_identity, secrets.chat_pin, chatxdk |
| lifecycle.chat.identity_recover | ChatSDKUnlock | POST | lifecycle.chat_identity_recover, secrets.chat_pin, chatxdk |
| lifecycle.chat.new_conversation | AddEncryptedConversationKeysMutation | POST | lifecycle.chat_new_conversation, samples.peer_username, samples.peer_user_id, chat_identity |
| lifecycle.chat.send_new | SendMessageCreateMutation | POST | lifecycle.chat_new_conversation, samples.peer_username, samples.peer_user_id, chat_identity |
| lifecycle.chat.send_existing | SendMessageCreateMutation | POST | lifecycle.chat_send, samples.peer_username, samples.peer_user_id, chat_identity |
| lifecycle.chat.readback | GetConversationPageQuery | GET | samples.peer_username, samples.peer_user_id, chat_identity |
