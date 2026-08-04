"""docs/llm-academy 结构测试：集合守恒、内部链接、FM 锚点、资源、Pages 工作流。"""
from __future__ import annotations

import hashlib
import os
import re
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DOCS = PACKAGE_ROOT / "docs" / "llm-academy"
README_ZH = PACKAGE_ROOT / "README_zh.md"
WORKFLOW = PACKAGE_ROOT / ".github" / "workflows" / "deploy-pages.yml"

# W0 输入锁冻结值（tree-sha256-v1，all 变体）
LOCKED_DIGEST = "d6f8c7d9c9a653c126a8307212592cc5012cdf774b6abf3752e67cd9e82d1fb0"
LOCKED_COUNT = 17


def tree_digest(root: Path):
    entries = []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            full = Path(dp) / f
            if full.is_symlink() or not full.is_file():
                continue
            rel = full.relative_to(root).as_posix()
            entries.append((rel, hashlib.sha256(full.read_bytes()).hexdigest()))
    entries.sort(key=lambda e: e[0].encode("utf-8"))
    acc = b""
    for rel, d in entries:
        acc += rel.encode("utf-8") + b"\x00" + d.encode("ascii") + b"\n"
    return len(entries), hashlib.sha256(acc).hexdigest()


class DocsAcademyTests(unittest.TestCase):
    def test_file_set_and_digest_are_locked(self) -> None:
        n, d = tree_digest(DOCS)
        self.assertEqual(n, LOCKED_COUNT)
        self.assertEqual(d, LOCKED_DIGEST, "文档集合与 W0 冻结摘要不一致")

    def test_internal_links_resolve(self) -> None:
        for html in DOCS.glob("*.html"):
            text = html.read_text(encoding="utf-8")
            for attr in ("href", "src"):
                for target in re.findall(rf'{attr}="([^"]+)"', text):
                    if target.startswith(("http://", "https://", "mailto:", "#", "javascript:")):
                        continue
                    path_part = target.split("#", 1)[0].split("?", 1)[0]
                    if not path_part:
                        continue
                    resolved = (html.parent / path_part).resolve()
                    self.assertTrue(resolved.is_file(), f"{html.name}: 断链 {target}")
                    self.assertIn(str(DOCS.resolve()), str(resolved), f"{html.name}: 越界 {target}")

    def test_fm_anchor_links_exist_in_handbook(self) -> None:
        handbook = (DOCS / "10-fm-handbook.html").read_text(encoding="utf-8")
        anchor_ids = set(re.findall(r'id="(fm-[^"]+)"', handbook)) | set(re.findall(r'name="(fm-[^"]+)"', handbook))
        self.assertGreater(len(anchor_ids), 0, "手册缺少 fm-* 锚点")
        referenced = set()
        for html in DOCS.glob("*.html"):
            text = html.read_text(encoding="utf-8")
            for target in re.findall(r'href="10-fm-handbook\.html#([^"]+)"', text):
                referenced.add(target)
        missing = referenced - anchor_ids
        self.assertFalse(missing, f"丢失 FM 锚点：{sorted(missing)}")

    def test_all_pages_are_utf8_and_assets_present(self) -> None:
        html_files = sorted(DOCS.glob("*.html"))
        self.assertEqual(len(html_files), 15)
        for html in html_files:
            html.read_text(encoding="utf-8")  # 解码失败即失败
        self.assertTrue((DOCS / "assets" / "style.css").is_file())
        self.assertTrue((DOCS / "assets" / "terms.js").is_file())
        users = [h.name for h in html_files if "assets/style.css" in h.read_text(encoding="utf-8")]
        self.assertGreater(len(users), 0)

    def test_readme_zh_index_links_resolve(self) -> None:
        text = README_ZH.read_text(encoding="utf-8")
        links = re.findall(r"\]\((docs/llm-academy/[^)#]+)", text)
        self.assertGreaterEqual(len(links), 15)
        for rel in links:
            self.assertTrue((PACKAGE_ROOT / rel).is_file(), f"README_zh 断链：{rel}")

    def test_pages_workflow_minimal_and_scoped(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("contents: read", text)
        self.assertIn("pages: write", text)
        self.assertIn("id-token: write", text)
        self.assertNotIn("contents: write", text)
        self.assertIn("path: docs/llm-academy", text)
        self.assertIn("actions/upload-pages-artifact", text)
        self.assertIn("actions/deploy-pages", text)
        self.assertNotIn("/Users/", text)


if __name__ == "__main__":
    unittest.main()
