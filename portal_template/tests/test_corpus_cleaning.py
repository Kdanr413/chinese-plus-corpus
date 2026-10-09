import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prepare" / "clean.py"
SPEC = importlib.util.spec_from_file_location("corpus_cleaning", MODULE_PATH)
corpus_cleaning = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(corpus_cleaning)


class HeadingDetectionTests(unittest.TestCase):
    def test_date_time_is_not_heading(self):
        self.assertIsNone(corpus_cleaning.heading_level_and_title("2023.04.21 22:20"))

    def test_long_legal_article_body_is_not_heading(self):
        text = "第五条 研究本行业的热点问题、共性问题和重大问题，组织行业交流和研讨，引领行业健康有序发展。"
        self.assertIsNone(corpus_cleaning.heading_level_and_title(text))

    def test_short_legal_article_title_is_heading(self):
        result = corpus_cleaning.heading_level_and_title("第五条 适用范围")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 2)
        self.assertEqual(result[2], "legal_article")

    def test_long_numbered_body_is_not_heading(self):
        text = "（一）新能源汽车生产企业应当建立回收服务网点，并按照要求及时上传动力电池全生命周期信息。"
        self.assertIsNone(corpus_cleaning.heading_level_and_title(text))

    def test_policy_heading_is_heading(self):
        result = corpus_cleaning.heading_level_and_title("二、提升新能源汽车产业创新能力")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 2)

    def test_numeric_table_row_is_not_heading(self):
        self.assertIsNone(
            corpus_cleaning.heading_level_and_title(
                "3 非化石能源消费比重 % 21.7 25 约束性"
            )
        )

    def test_table_of_contents_line_is_not_heading(self):
        self.assertIsNone(
            corpus_cleaning.heading_level_and_title(
                "1.1.3 高压配电箱低压接插件端子定义 4…………"
            )
        )

    def test_chapter_number_without_title_is_not_heading(self):
        self.assertIsNone(corpus_cleaning.heading_level_and_title("第1章"))


class CrawlerCleaningTests(unittest.TestCase):
    def test_caam_main_content_boundary(self):
        raw = """URL: https://example.test
TITLE: 新能源汽车动力电池管理办法
DATE: 2026-01-01

首页
协会概况
正文
新能源汽车动力电池管理办法
第一章 总则
本办法适用于新能源汽车动力电池。
最新新闻
无关新闻标题
友情链接
"""
        metadata = corpus_cleaning.extract_header_metadata(raw)
        selected = corpus_cleaning.select_crawler_main_content(
            raw, "caam", metadata["title"]
        )
        self.assertIn("动力电池", selected)
        self.assertNotIn("无关新闻标题", selected)
        self.assertNotIn("友情链接", selected)

    def test_ccgp_related_articles_are_removed(self):
        raw = """URL: https://example.test
TITLE: 新能源汽车政府采购需求标准_中国政府采购网
DATE: 2026-01-01

首页
当前位置：首页政府采购动态
新能源汽车政府采购需求标准
本文讨论新能源汽车政府采购需求标准和充电指标。
相关文章
物业管理服务采购标准
采购头条
"""
        metadata = corpus_cleaning.extract_header_metadata(raw)
        selected = corpus_cleaning.select_crawler_main_content(
            raw, "ccgp", metadata["title"]
        )
        self.assertIn("充电指标", selected)
        self.assertNotIn("物业管理", selected)

    def test_domain_relevance_rejects_unrelated_policy(self):
        result = corpus_cleaning.crawler_domain_relevance(
            "农业农村现代化规划",
            "粮食生产、耕地保护、乡村振兴和农业科技是本规划的重点内容。",
        )
        self.assertFalse(result["relevant"])

    def test_domain_relevance_accepts_nev_title(self):
        result = corpus_cleaning.crawler_domain_relevance(
            "新能源汽车动力电池回收管理办法",
            "本办法规范动力电池回收和新能源汽车生产企业责任。",
        )
        self.assertTrue(result["relevant"])


class ExerciseRemovalTests(unittest.TestCase):
    def test_removes_exercise_block_until_next_peer_heading(self):
        paragraphs = [
            "一、动力电池基本原理",
            "动力电池由电芯和电池管理系统组成。",
            "二、练习题",
            "1. 简述动力电池的作用。",
            "2. 什么是荷电状态？",
            "三、充电系统",
            "充电系统包括车载充电机和充电接口。",
        ]
        kept, removed = corpus_cleaning.remove_exercise_blocks(paragraphs)
        self.assertGreaterEqual(removed, 3)
        self.assertIn("三、充电系统", kept)
        self.assertNotIn("1. 简述动力电池的作用。", kept)

    def test_removes_repeated_running_chapter_header(self):
        paragraphs = [
            "第一章 动力电池",
            "动力电池由若干电芯组成。",
            "第一章 动力电池",
            "电池管理系统负责状态监测。",
        ]
        kept, removed = corpus_cleaning.remove_repeated_running_headers(paragraphs)
        self.assertEqual(removed, 1)
        self.assertEqual(kept.count("第一章 动力电池"), 1)


class TextGenreTests(unittest.TestCase):
    def test_multiple_speakers_are_dialogue(self):
        result = corpus_cleaning.detect_text_genre(
            "充电站情景对话",
            ["李老师：请先关闭电源。", "小王：已经关闭了。", "李老师：现在检查充电接口。"],
        )
        self.assertEqual(result["text_genre"], "对话")
        self.assertEqual(result["genre_evidence"]["distinct_speaker_count"], 2)

    def test_prose_is_narrative(self):
        result = corpus_cleaning.detect_text_genre(
            "动力电池管理办法",
            ["动力电池生产企业应当建立产品追溯制度。", "本办法自发布之日起施行。"],
        )
        self.assertEqual(result["text_genre"], "记叙文")

    def test_explanatory_colons_are_not_speakers(self):
        result = corpus_cleaning.detect_text_genre(
            "技术说明",
            ["说明：本系统应保持断电。", "示例：充电前检查接口。", "备注：禁止带电操作。"],
        )
        self.assertEqual(result["text_genre"], "记叙文")


if __name__ == "__main__":
    unittest.main()
