import urllib.error
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from corpus_api.topic_workflow import _call
from corpus_topic_workflow.llm_annotation_api import _request_json,ModelAPITimeoutError

class TopicModelErrorsTests(unittest.TestCase):
    def test_read_timeout_becomes_json_compatible_504(self):
        with patch('corpus_topic_workflow.llm_annotation_api.urllib.request.urlopen',side_effect=TimeoutError('read timed out')):
            with self.assertRaises(HTTPException) as failure:
                _call(_request_json,{'timeout_seconds':120},'PRIVATE-KEY','https://model.example/v1',{})
        self.assertEqual(failure.exception.status_code,504)
        self.assertIn('120',failure.exception.detail)
        self.assertNotIn('PRIVATE-KEY',failure.exception.detail)

    def test_connection_timeout_and_unavailable_api_are_distinct(self):
        with patch('corpus_topic_workflow.llm_annotation_api.urllib.request.urlopen',side_effect=urllib.error.URLError(TimeoutError())):
            with self.assertRaises(ModelAPITimeoutError):
                _request_json({'timeout_seconds':60},'key','https://model.example/v1',{})
        with self.assertRaises(HTTPException) as failure:
            _call(lambda:(_ for _ in ()).throw(RuntimeError('模型不可用')))
        self.assertEqual(failure.exception.status_code,502)
