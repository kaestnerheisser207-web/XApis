"""Public SDK error decisions, including secret-bearing transport failures."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from types import SimpleNamespace
import unittest

from curl_cffi.requests import exceptions as curl_errors
from requests import exceptions as request_errors

from x_apis.client_errors import XClientError, api_method, graphql_error, normalize_error
from x_apis.errors import GraphQLError


SECRET = 'PRIVATE_TOKEN https://user:password@proxy.invalid/private'


@api_method
def raise_from_sdk(error):
    raise error


class ClientErrorTests(unittest.TestCase):
    def assert_public(self, error, code):
        with self.assertRaises(XClientError) as caught:
            raise_from_sdk(error)
        actual = caught.exception
        self.assertEqual(actual.code, code)
        self.assertFalse(actual.retryable)
        self.assertNotIn('PRIVATE_TOKEN', str(actual))
        self.assertNotIn('password', str(actual))
        self.assertNotIn('proxy.invalid', str(actual))
        self.assertIsNone(actual.__cause__)
        self.assertTrue(actual.__suppress_context__)
        return actual

    def test_existing_error_and_partial_result_are_preserved(self):
        result = {'post_ids': ['9001'], 'status': 'partial'}
        error = XClientError('thread_incomplete', '串文未完成', result=result)
        self.assertIs(normalize_error(error), error)
        with self.assertRaises(XClientError) as caught:
            raise_from_sdk(error)
        self.assertIs(caught.exception, error)
        self.assertIs(error.result, result)
        self.assertFalse(error.retryable)
        self.assertIsNone(error.status_code)
        self.assertIsNone(error.retry_after)
        self.assertEqual(error.upstream_codes, ())

    def test_graphql_auth_rate_limit_and_risk_are_distinct(self):
        for code, expected in [(32, 'auth_required'), (89, 'auth_required'),
                               (135, 'auth_required'), (88, 'rate_limited'),
                               (226, 'risk_controlled'), (999, 'upstream_error')]:
            with self.subTest(code=code):
                error = self.assert_public(
                    GraphQLError('Synthetic', [{'code': code, 'message': SECRET}],
                                 {'private': SECRET}), expected)
                self.assertEqual(error.upstream_codes, (code,))
                self.assertIsNone(error.result)

    def test_graphql_codes_are_bounded_numeric_metadata(self):
        errors = [{'code': code, 'message': SECRET}
                  for code in ['88', 88, True, 88.0, SECRET, '８８', '1234567', -1]]
        actual = graphql_error(errors)
        self.assertEqual(actual.code, 'rate_limited')
        self.assertEqual(actual.upstream_codes, (88,))
        self.assertNotIn(SECRET, repr(vars(actual)))

    def test_multiple_graphql_errors_prioritize_account_intervention(self):
        error = graphql_error([{'code': 88}, {'code': 226}, {'code': 32}])
        self.assertEqual(error.code, 'auth_required')
        self.assertEqual(error.upstream_codes, (32, 88, 226))
        self.assertEqual(graphql_error([{'code': 88}, {'code': 226}]).code,
                         'risk_controlled')

    def test_malformed_graphql_errors_are_response_errors(self):
        for errors in (None, {}, SECRET, [], [None], [SECRET]):
            with self.subTest(errors=type(errors).__name__):
                actual = graphql_error(errors)
                self.assertEqual(actual.code, 'invalid_response')
                self.assertFalse(actual.retryable)
                self.assertNotIn(SECRET, str(actual))

    def test_http_statuses_use_both_supported_transports(self):
        for error_type in (request_errors.HTTPError, curl_errors.HTTPError):
            for status, expected in [(401, 'auth_required'), (403, 'forbidden'),
                                     (429, 'rate_limited'), (500, 'upstream_unavailable'),
                                     (503, 'upstream_unavailable'), (400, 'upstream_error')]:
                with self.subTest(error_type=error_type.__module__, status=status):
                    response = SimpleNamespace(status_code=status, headers={}, text=SECRET)
                    actual = self.assert_public(error_type(SECRET, response=response), expected)
                    self.assertEqual(actual.status_code, status)
                    self.assertIsNone(actual.result)

    def test_falsey_http_error_response_is_still_classified(self):
        class Response:
            status_code = 429
            headers = {'retry-after': '60'}

            def __bool__(self):
                return False

        actual = self.assert_public(request_errors.HTTPError(SECRET, response=Response()),
                                    'rate_limited')
        self.assertEqual(actual.retry_after, 60)

    def test_retry_after_supports_seconds_and_http_date(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        for value in ('60', ' 60 ', format_datetime(future, usegmt=True)):
            response = SimpleNamespace(status_code=429, headers={'Retry-After': value})
            actual = self.assert_public(request_errors.HTTPError(SECRET, response=response),
                                        'rate_limited')
            self.assertGreaterEqual(actual.retry_after, 60)
            self.assertLessEqual(actual.retry_after, 120)
        response.headers = {'retry-after': 'Thu, 01 Jan 1970 00:00:00 GMT'}
        self.assertEqual(normalize_error(request_errors.HTTPError(SECRET, response=response))
                         .retry_after, 0)

    def test_untrusted_retry_after_is_not_exposed(self):
        for value in (SECRET, '-1', 'NaN', 'Infinity', '1e9', '９０', '9' * 100,
                      '2147483648', True, 60, None, 'Thu, 01 Jan 2027 00:00:00'):
            with self.subTest(value_type=type(value).__name__):
                response = SimpleNamespace(status_code=429, headers={'retry-after': value})
                actual = self.assert_public(request_errors.HTTPError(SECRET, response=response),
                                            'rate_limited')
                self.assertIsNone(actual.retry_after)

    def test_timeout_and_network_failures_never_authorize_write_retries(self):
        for error_type in (TimeoutError, request_errors.Timeout, request_errors.ConnectTimeout,
                           curl_errors.Timeout, curl_errors.ReadTimeout):
            with self.subTest(error_type=error_type.__name__):
                self.assert_public(error_type(SECRET), 'timeout')
        for error_type in (ConnectionError, request_errors.ConnectionError,
                           request_errors.ProxyError, curl_errors.ConnectionError,
                           curl_errors.ProxyError, curl_errors.DNSError):
            with self.subTest(error_type=error_type.__name__):
                self.assert_public(error_type(SECRET), 'network_error')

    def test_timeout_after_response_headers_remains_timeout(self):
        response = SimpleNamespace(status_code=200, headers={})
        self.assert_public(curl_errors.ReadTimeout(SECRET, response=response), 'timeout')

    def test_invalid_arguments_are_distinct_from_response_structure(self):
        for error in (ValueError(SECRET), TypeError(SECRET)):
            self.assert_public(error, 'invalid_argument')
        for error in (KeyError(SECRET), AttributeError(SECRET),
                      json.JSONDecodeError(SECRET, '{}', 1),
                      request_errors.JSONDecodeError(SECRET, '{}', 1)):
            self.assert_public(error, 'invalid_response')

    def test_unrecognized_errors_and_identity_errors_are_sanitized(self):
        from utils.xchat_identity import IdentityError
        self.assert_public(RuntimeError(SECRET), 'operation_failed')
        self.assert_public(OSError(SECRET), 'operation_failed')
        self.assert_public(IdentityError(SECRET), 'identity_error')


if __name__ == '__main__':
    unittest.main()
