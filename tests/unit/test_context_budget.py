"""验证原生框架文档进入 Context 时保留完整证据和实际来源。"""

import unittest

from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda

from workstation_agent.context import ContextBuilder
from workstation_agent.context.evidence import assemble_evidence


def fragment(identifier, content, begin=0):
    """构造有来源范围的证据夹具。"""
    return Document(
        id=identifier,
        page_content=content,
        metadata={
            "document_id": "doc",
            "source_uri": "a.md",
            "title": "A",
            "checksum": "v",
            "section_id": "section",
            "heading_path": None,
            "source_start": begin,
            "source_end": begin + len(content),
            "line_start": 1,
            "line_end": 1,
            "retrieval_score": 1.0,
        },
    )


class ContextBudgetTests(unittest.TestCase):
    def test_budget_drops_whole_evidence_and_tracks_only_used_ids(self):
        retriever = RunnableLambda(
            lambda _: [
                fragment("one", "short"),
                fragment("two", "long" * 100),
            ]
        )
        messages, sources = ContextBuilder(retriever=retriever, max_context_chars=100).build(
            "query", "system"
        )
        self.assertEqual(sources.chunk_ids, ("one",))
        self.assertIn("short", messages[1].content)
        self.assertNotIn("long", messages[1].content)
        self.assertNotIn("truncated", messages[1].content)

    def test_expansion_does_not_discard_seed_and_bridging_overlap_is_deduplicated(self):
        original = "abcdefghij"
        documents = [
            fragment("one", original[:4]),
            fragment("two", original[6:], 6),
            fragment("bridge", original[3:7], 3),
        ]
        evidence, context, omitted = assemble_evidence(documents, 1000, measure=len)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["content"], original)
        self.assertEqual(set(evidence[0]["chunk_ids"]), {"one", "two", "bridge"})
        seed = [fragment("one", original[:4])]
        evidence, context, omitted = assemble_evidence(seed, 1000, measure=len)
        evidence, context, omitted = assemble_evidence(
            seed + [fragment("extra", original[4:], 4)],
            len(context),
            measure=len,
        )
        self.assertTrue(omitted)
        self.assertEqual(evidence[0]["content"], "abcd")
        self.assertEqual(evidence[0]["chunk_ids"], ["one"])
