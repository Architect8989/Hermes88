# Deep Research Skill

## Trigger
User asks for research on a topic, company, technology, or competitive analysis.

## Protocol
1. Run 3-5 web searches with different query angles
2. web_fetch the top 2 results from each search
3. Synthesize into structured report: Summary → Key Findings → Sources
4. Save report to /data/.hermes/research/YYYYMMDD_topic.md
5. Return summary to Telegram. Offer full report on request.

## Never
- Return search result snippets without fetching full pages
- Make claims not supported by fetched content
- Take longer than 10 minutes (abort if hitting this)

## Context file before delegating
cat > /tmp/task_context.md << 'CTX'
TASK: Deep research on [TOPIC]
QUERY_ANGLES: [list 3-5 distinct search angles]
OUTPUT_PATH: /data/.hermes/research/YYYYMMDD_[topic].md
CTX
