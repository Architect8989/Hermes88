#!/usr/bin/env python3
"""
Document Generation Skill for Hermes88.
Generates professional documents from templates: pitch decks, proposals,
technical design docs, audit reports, and investor updates.

Supports Markdown, PDF (via pandoc/weasyprint), and HTML output.

Rhodawk AI -- Peak Architecture v10.0
"""
import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import urllib.error
import urllib.request


@dataclass
class DocumentRequest:
    """Specification for a document to generate."""
    doc_type: str = "general"
    topic: str = ""
    format: str = "markdown"
    output_path: str = ""
    context: dict = field(default_factory=dict)
    sections: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


# -- Document Templates -----------------------------------------------------------

TEMPLATES = {
    "pitch-deck": {
        "title": "Investor Pitch Deck",
        "sections": [
            "Title Slide (Company + Tagline + Logo)",
            "Problem (Market pain point with data)",
            "Solution (Product overview + demo screenshot)",
            "Market Size (TAM/SAM/SOM with sources)",
            "Business Model (Revenue streams + pricing)",
            "Traction (Key metrics, growth rate, logos)",
            "Competition (Competitive matrix, differentiation)",
            "Team (Founders + key hires + advisors)",
            "Financials (Revenue, burn rate, runway)",
            "The Ask (Raise amount, use of funds, timeline)",
        ],
        "format_instructions": (
            "Each section should be 3-5 bullet points maximum. "
            "Data-driven. No fluff. Investor-ready density. "
            "Include specific numbers where available from context."
        ),
    },
    "proposal": {
        "title": "Client Proposal",
        "sections": [
            "Executive Summary",
            "Problem Statement",
            "Proposed Solution",
            "Technical Approach",
            "Timeline and Milestones",
            "Pricing and Terms",
            "Team and Qualifications",
            "Next Steps",
        ],
        "format_instructions": (
            "Professional tone. Specific deliverables. "
            "Clear pricing tiers. Realistic timeline."
        ),
    },
    "tech-design": {
        "title": "Technical Design Document",
        "sections": [
            "RFC Summary (one paragraph)",
            "Motivation and Background",
            "Goals and Non-Goals",
            "Proposed Design",
            "Architecture Diagram",
            "API / Interface Changes",
            "Data Model Changes",
            "Security Considerations",
            "Testing Strategy",
            "Rollout Plan",
            "Open Questions",
        ],
        "format_instructions": (
            "RFC style. Specific enough to implement from. "
            "Include code examples where relevant. "
            "List explicit non-goals to prevent scope creep."
        ),
    },
    "audit-report": {
        "title": "Security Audit Report",
        "sections": [
            "Executive Summary (risk level + key findings count)",
            "Scope (repos audited, tools used, date range)",
            "Critical Findings",
            "High-Severity Findings",
            "Medium-Severity Findings",
            "Low-Severity Findings",
            "Recommendations (prioritized)",
            "Remediation Timeline",
        ],
        "format_instructions": (
            "Each finding: title, description, impact, remediation. "
            "CVSS scores where applicable. "
            "Prioritized recommendations with effort estimates."
        ),
    },
    "weekly-report": {
        "title": "Weekly Investor Update",
        "sections": [
            "TL;DR (3 bullets: win, challenge, ask)",
            "Metrics Dashboard (MRR, users, growth %)",
            "Wins This Week",
            "Challenges and Blockers",
            "Next Week Priorities",
            "Burn and Runway Update",
            "Asks (introductions, advice, resources)",
        ],
        "format_instructions": (
            "Brief and scannable. Investors read 50+ updates. "
            "Lead with metrics. Be honest about challenges. "
            "Specific asks that investors can act on."
        ),
    },
    "changelog": {
        "title": "Release Notes",
        "sections": [
            "Version and Date",
            "Breaking Changes",
            "New Features",
            "Bug Fixes",
            "Performance Improvements",
            "Deprecations",
            "Upgrade Instructions",
        ],
        "format_instructions": (
            "Keep It Simple. One line per change. "
            "Group by type. Link to issues/PRs."
        ),
    },
}


class LLMDocumentWriter:
    """Uses LLM to generate document content from templates and context."""

    def __init__(self):
        """Initialize with API credentials from environment."""
        self.api_key = os.environ.get("DO_INFERENCE_API_KEY", "")
        self.base_url = os.environ.get(
            "DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1"
        )
        self.model = os.environ.get("HERMES_MODEL", "deepseek-v4-pro")

    def generate_content(self, doc_type: str, topic: str,
                         context: dict = None) -> str:
        """
        Generate document content using LLM.

        Args:
            doc_type: Type of document (pitch-deck, proposal, etc.)
            topic: Main topic/subject.
            context: Additional context (data points, constraints).

        Returns:
            Generated markdown content.
        """
        template = TEMPLATES.get(doc_type, TEMPLATES.get("general", {
            "title": "Document",
            "sections": ["Content"],
            "format_instructions": "Write clearly and concisely.",
        }))

        system_prompt = (
            f"You are a professional document writer for Rhodawk AI.\n"
            f"Generate a {template['title']} about: {topic}\n\n"
            f"Structure:\n"
            + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(template["sections"]))
            + f"\n\nFormat: Markdown\n"
            f"Instructions: {template['format_instructions']}\n\n"
            f"Write the complete document now. Use ## for section headers. "
            f"Be specific and data-driven. No placeholder text."
        )

        user_msg = f"Topic: {topic}"
        if context:
            user_msg += f"\n\nContext:\n{json.dumps(context, indent=2)}"

        response = self._call_llm(system_prompt, user_msg)
        if response:
            return response

        # Fallback: generate skeleton from template
        return self._generate_skeleton(template, topic)

    def _call_llm(self, system: str, user: str) -> Optional[str]:
        """Call LLM API."""
        if not self.api_key:
            return None

        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user[:20000]},
            ],
            "temperature": 0.3,
            "max_tokens": 8192,
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[doc_gen] LLM error: {e}", flush=True)
            return None

    def _generate_skeleton(self, template: dict, topic: str) -> str:
        """Generate a document skeleton without LLM."""
        lines = [f"# {template['title']}: {topic}\n"]
        lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n")

        for section in template["sections"]:
            lines.append(f"\n## {section}\n")
            lines.append("[Content to be filled]\n")

        return "\n".join(lines)


class DocumentFormatter:
    """Converts markdown documents to other formats."""

    def __init__(self):
        """Check available conversion tools."""
        self.pandoc_available = self._check_tool("pandoc")
        self.weasyprint_available = self._check_tool("weasyprint")

    def _check_tool(self, tool: str) -> bool:
        """Check if a tool is available."""
        try:
            result = subprocess.run(
                [tool, "--version"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def to_pdf(self, markdown_content: str, output_path: str) -> bool:
        """
        Convert markdown to PDF.

        Args:
            markdown_content: Source markdown text.
            output_path: Destination PDF path.

        Returns:
            True on success.
        """
        # Try pandoc first
        if self.pandoc_available:
            return self._pandoc_to_pdf(markdown_content, output_path)

        # Try weasyprint (HTML -> PDF)
        if self.weasyprint_available:
            return self._weasyprint_to_pdf(markdown_content, output_path)

        print("[doc_gen] No PDF converter available (pandoc or weasyprint)", flush=True)
        # Fallback: save as markdown
        md_path = output_path.replace(".pdf", ".md")
        Path(md_path).write_text(markdown_content)
        print(f"[doc_gen] Saved as markdown: {md_path}", flush=True)
        return False

    def to_html(self, markdown_content: str, output_path: str) -> bool:
        """Convert markdown to HTML."""
        # Simple markdown to HTML conversion
        html = self._markdown_to_html(markdown_content)

        full_html = (
            "<!DOCTYPE html>\n<html>\n<head>\n"
            '<meta charset="utf-8">\n'
            "<style>\n"
            "body { font-family: -apple-system, sans-serif; max-width: 800px; "
            "margin: 0 auto; padding: 40px; line-height: 1.6; }\n"
            "h1 { border-bottom: 2px solid #333; padding-bottom: 10px; }\n"
            "h2 { color: #333; margin-top: 30px; }\n"
            "code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px; }\n"
            "pre { background: #f4f4f4; padding: 16px; border-radius: 6px; "
            "overflow-x: auto; }\n"
            "table { border-collapse: collapse; width: 100%; }\n"
            "th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }\n"
            "</style>\n</head>\n<body>\n"
            f"{html}\n</body>\n</html>"
        )

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(full_html)
        return True

    def _pandoc_to_pdf(self, content: str, output_path: str) -> bool:
        """Convert via pandoc."""
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            md_path = f.name

        try:
            result = subprocess.run(
                ["pandoc", md_path, "-o", output_path,
                 "--pdf-engine=xelatex",
                 "-V", "geometry:margin=1in"],
                capture_output=True, text=True, timeout=60,
            )
            return result.returncode == 0
        except Exception as e:
            print(f"[doc_gen] Pandoc error: {e}", flush=True)
            return False
        finally:
            Path(md_path).unlink(missing_ok=True)

    def _weasyprint_to_pdf(self, content: str, output_path: str) -> bool:
        """Convert via weasyprint (HTML intermediate)."""
        import tempfile
        html = self._markdown_to_html(content)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(f"<html><body>{html}</body></html>")
            html_path = f.name

        try:
            result = subprocess.run(
                ["weasyprint", html_path, output_path],
                capture_output=True, timeout=60,
            )
            return result.returncode == 0
        except Exception as e:
            print(f"[doc_gen] Weasyprint error: {e}", flush=True)
            return False
        finally:
            Path(html_path).unlink(missing_ok=True)

    def _markdown_to_html(self, content: str) -> str:
        """Simple markdown to HTML conversion."""
        import re
        html = content

        # Headers
        html = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
        html = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
        html = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html, flags=re.MULTILINE)

        # Bold and italic
        html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
        html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)

        # Code blocks
        html = re.sub(
            r'```(\w*)\n(.*?)```',
            r'<pre><code>\2</code></pre>',
            html, flags=re.DOTALL,
        )

        # Inline code
        html = re.sub(r'`([^`]+)`', r'<code>\1</code>', html)

        # Lists
        html = re.sub(r'^- (.+)$', r'<li>\1</li>', html, flags=re.MULTILINE)

        # Paragraphs
        html = re.sub(r'\n\n', '</p><p>', html)
        html = f"<p>{html}</p>"

        return html


class DocumentGenerator:
    """
    Main document generation orchestrator.
    Combines LLM content generation with format conversion.
    """

    def __init__(self):
        """Initialize generator components."""
        self.writer = LLMDocumentWriter()
        self.formatter = DocumentFormatter()

    def generate(self, request: DocumentRequest) -> str:
        """
        Generate a complete document.

        Args:
            request: DocumentRequest specifying type, topic, format, etc.

        Returns:
            Path to generated document file.
        """
        print(f"[doc_gen] Generating: {request.doc_type} about '{request.topic}'", flush=True)
        print(f"[doc_gen] Format: {request.format}", flush=True)

        # Step 1: Generate content
        content = self.writer.generate_content(
            doc_type=request.doc_type,
            topic=request.topic,
            context=request.context,
        )

        if not content:
            print("[doc_gen] Content generation failed", flush=True)
            return ""

        # Step 2: Determine output path
        if request.output_path:
            output_path = request.output_path
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            slug = request.topic[:30].replace(" ", "_").lower()
            slug = "".join(c for c in slug if c.isalnum() or c == "_")
            ext = {"markdown": ".md", "pdf": ".pdf", "html": ".html"}.get(
                request.format, ".md"
            )
            output_path = f"/tmp/hermes_doc_{slug}_{timestamp}{ext}"

        # Step 3: Convert to requested format
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if request.format == "pdf":
            success = self.formatter.to_pdf(content, output_path)
            if not success:
                # Fallback to markdown
                output_path = output_path.replace(".pdf", ".md")
                Path(output_path).write_text(content)
        elif request.format == "html":
            self.formatter.to_html(content, output_path)
        else:
            # Default: markdown
            if not output_path.endswith(".md"):
                output_path += ".md"
            Path(output_path).write_text(content)

        print(f"[doc_gen] Generated: {output_path}", flush=True)
        return output_path

    def list_types(self) -> list:
        """List available document types."""
        return list(TEMPLATES.keys())


# -- CLI Interface ---------------------------------------------------------------

def main():
    """CLI entry point for document generator."""
    parser = argparse.ArgumentParser(
        description="Document Generator -- Rhodawk AI Hermes88"
    )
    parser.add_argument(
        "--type", default="general",
        choices=list(TEMPLATES.keys()) + ["general"],
        help="Document type"
    )
    parser.add_argument("--topic", required=True, help="Document topic/subject")
    parser.add_argument(
        "--format", default="markdown",
        choices=["markdown", "pdf", "html"],
        help="Output format"
    )
    parser.add_argument("--output", default="", help="Output file path")
    parser.add_argument(
        "--context", default="{}",
        help="JSON context string with additional data"
    )

    args = parser.parse_args()

    generator = DocumentGenerator()
    request = DocumentRequest(
        doc_type=args.type,
        topic=args.topic,
        format=args.format,
        output_path=args.output,
        context=json.loads(args.context) if args.context else {},
    )

    output_path = generator.generate(request)
    if output_path:
        print(f"Document saved: {output_path}")
    else:
        print("Document generation failed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
