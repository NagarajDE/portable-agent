You are GATHERING EVIDENCE to answer a QUESTION. You may call READ-ONLY tools to fetch data.
Do NOT answer the question here — a later step writes the answer from the evidence you gather.

Reply with EXACTLY ONE JSON object and nothing else:
  - to call a tool:            {"tool": "<name>", "input": { ... }}
  - when you have enough:      {"final": true}

Rules: call a tool only if it adds evidence you don't already have; never repeat the same tool with
the same input; if no tool would help, reply {"final": true}.

QUESTION:
{task}

AVAILABLE TOOLS:
{tools}

EVIDENCE GATHERED SO FAR:
{observations}
