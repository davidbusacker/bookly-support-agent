import {
  Callout,
  Card,
  CardBody,
  CardHeader,
  Code,
  CollapsibleSection,
  Divider,
  H1,
  H2,
  Stack,
  Text,
} from "cursor/canvas";

const CARDS: Array<{ q: string; a: string; proof: string }> = [
  {
    q: "1. Where do business rules live versus runtime enforcement?",
    a: "Policies (who Riley is, how returns work, when loyalty applies) live in markdown AOPs. Python does not decide “3 orders in 365 days” — Riley reads that AOP and counts with tools. Python does enforce things the model must not be trusted with: phone last-4, blocking account tools, and the intent/resolution cutoffs. You can change refund policy by editing aops/ and restarting, unless it is one of those hard gates.",
    proof: "aops/*.md · aop_loader.py (format_aop_catalog, build_system_prompt) · tools.py (requires_authentication) · agent/pipeline.py (thresholds)",
  },
  {
    q: "2. Walk me through one customer turn. What runs before Riley speaks?",
    a: "The browser hits /api/chat. The server loads the Session, refreshes prior-trace summaries, then chat_events classifies intent (Haiku). If confidence is below the env cutoff, Riley never runs — a clarifying question is generated instead. Otherwise the new user line is appended to session.messages and run_agent_turn starts the Claude tool loop. After she replies, the orchestrator may score resolution and ask if everything is resolved. Restock only happens after confirm.",
    proof: "_infra/server.py chat() → agent/pipeline.py chat_events() → agent/loop.py run_agent_turn()",
  },
  {
    q: "3. What is a tool-use loop, and when does it stop?",
    a: "Riley does not call Bookly herself. She requests a tool name. run_agent_turn sends history + system prompt + TOOLS_SCHEMA to Claude. If stop reason is tool_use, tools.py runs each call, appends JSON as tool_result, and Claude is called again. That repeats until Claude returns plain text. The peek panel is those inner steps, not extra models.",
    proof: "agent/loop.py — while True, stop_reason == tool_use, execute_tool",
  },
  {
    q: "4. How do you stop the model from leaking account data before verification?",
    a: "Two layers. The identity AOP tells her not to reveal data until last-4 matches. The runtime blocks get_order, refunds, and similar tools until session.authenticated is true, returning authentication_required. Public tools (policies, FAQs, catalog, read_aop, verify_phone_last_four) stay allowed. Phone verify is a skill that returns only pass/fail; Claude never sees the phone number.",
    proof: "aops/identity-verification.md · tools.py PUBLIC_TOOLS + execute_tool · skills.py verify_phone_last_four",
  },
  {
    q: "5. What’s the difference between an AOP, a skill, and a Bookly tool?",
    a: "An AOP is a playbook. A skill is a local helper this repo owns (read_aop, verify_phone_last_four) because the model should not invent policy text or compare digits itself. A Bookly tool is a live store action from MCP (create_return, get_order). Everything Riley can choose goes through tools.py. Intent, resolution, and restock classifiers are not skills — the orchestrator calls them; they are not on the tool menu.",
    proof: "skills.py · bookly_client.py · guardrails.py · tools.py TOOLS_SCHEMA",
  },
  {
    q: "6. How does Riley know which extra policies exist, and when does she read them?",
    a: "Startup AOPs (startup: true in frontmatter) are inlined in the system prompt every turn. Every AOP’s title and description is also in the catalog, so she sees when to load on-demand ones. She only gets the full text of returns-and-refunds or loyalty-early-refund if she calls read_aop during the tool loop. Code does not auto-pick those files. Restock policy is also loaded by the orchestrator when it runs the restock check.",
    proof: "aop_loader.py format_aop_catalog() + build_system_prompt() · tools.py name == read_aop",
  },
  {
    q: "7. Where is the 50% / 90% cutoff actually enforced?",
    a: "Only in the loop. Haiku reports a 0–1 intent confidence and a 0–1 resolution score; prompts tell it not to apply a pass/fail cutoff. pipeline.py compares those numbers to INTENT_CONFIDENCE_THRESHOLD and RESOLUTION_THRESHOLD from .env / agent/config.py. Changing the env var changes behavior. Riley’s AOPs do not re-state 50% or 90%.",
    proof: "agent/pipeline.py (confidence < INTENT_CONFIDENCE_THRESHOLD, score >= RESOLUTION_THRESHOLD) · agent/config.py",
  },
  {
    q: "8. Why isn’t restock a tool Riley can call whenever she wants?",
    a: "Because then she could pitch mid-issue. After a high resolution score, the orchestrator asks if everything is resolved. Only after the customer confirms (or already said they’re good) does maybe_restock_offer look at prior traces and inventory. Riley is told not to pitch during an open issue. That is a sequence the agent cannot skip.",
    proof: "agent/pipeline.py maybe_restock_offer / confirm path · aops/restock-offer.md · aops/core.md close-out",
  },
  {
    q: "9. What does the model remember next turn?",
    a: "Same-tab memory is session.messages: old user and assistant turns plus tool calls. That whole list is passed into the next Claude call. Old tool JSON is trimmed (compact_history keeps the last two results). Auth, email, and order sit on the Session object and are re-injected in system_prompt(). Other Bookly conversations are a second channel: refresh_caller_history loads prior traces into caller_history on the system prompt, not as this tab’s transcript.",
    proof: "agent/pipeline.py append + run_agent_turn(session.messages) · agent/loop.py compact_history · agent/session.py system_prompt / refresh_caller_history",
  },
  {
    q: "10. If Bookly adds a new API, or you add a new policy, what do you change?",
    a: "New store capability: it appears on Bookly MCP; bookly_client.py lists it into TOOLS_SCHEMA after restart (unless you also need it on PUBLIC_TOOLS). New support policy: add aops/something.md with title / description frontmatter; the catalog and read_aop enum update from the files — you do not edit core.md as an index. A new “must never skip” rule (like auth) still belongs in Python, not only markdown.",
    proof: "aops/README.md · aop_loader.py · tools.py PUBLIC_TOOLS · bookly_client.py from_env / anthropic_tools()",
  },
];

export default function DecagonStudyCards() {
  return (
    <Stack gap={20}>
      <Stack gap={8}>
        <H1>Decagon study cards</H1>
        <Text tone="secondary">
          Ten questions a solution engineer might use to test whether you
          built a policy-driven agent with a real orchestrator. Expand a card
          to see the answer, then the file that proves it.
        </Text>
      </Stack>

      <Callout tone="info" title="If they only have time for three">
        Cards 1 (policy vs code), 4 (auth is a lock, not a suggestion), and 7
        (cutoffs live in the orchestrator). That is the Decagon story of this
        repo.
      </Callout>

      <H2>Quiz — question first</H2>
      <Text size="small" tone="tertiary">
        Sections start closed. Open one, say the answer out loud, then check.
      </Text>

      <CollapsibleSection title={CARDS[0].q}>
        <Stack gap={10}>
          <Text>{CARDS[0].a}</Text>
          <Text size="small" tone="secondary">
            Proof: <Code>{CARDS[0].proof}</Code>
          </Text>
        </Stack>
      </CollapsibleSection>
      <CollapsibleSection title={CARDS[1].q}>
        <Stack gap={10}>
          <Text>{CARDS[1].a}</Text>
          <Text size="small" tone="secondary">
            Proof: <Code>{CARDS[1].proof}</Code>
          </Text>
        </Stack>
      </CollapsibleSection>
      <CollapsibleSection title={CARDS[2].q}>
        <Stack gap={10}>
          <Text>{CARDS[2].a}</Text>
          <Text size="small" tone="secondary">
            Proof: <Code>{CARDS[2].proof}</Code>
          </Text>
        </Stack>
      </CollapsibleSection>
      <CollapsibleSection title={CARDS[3].q}>
        <Stack gap={10}>
          <Text>{CARDS[3].a}</Text>
          <Text size="small" tone="secondary">
            Proof: <Code>{CARDS[3].proof}</Code>
          </Text>
        </Stack>
      </CollapsibleSection>
      <CollapsibleSection title={CARDS[4].q}>
        <Stack gap={10}>
          <Text>{CARDS[4].a}</Text>
          <Text size="small" tone="secondary">
            Proof: <Code>{CARDS[4].proof}</Code>
          </Text>
        </Stack>
      </CollapsibleSection>
      <CollapsibleSection title={CARDS[5].q}>
        <Stack gap={10}>
          <Text>{CARDS[5].a}</Text>
          <Text size="small" tone="secondary">
            Proof: <Code>{CARDS[5].proof}</Code>
          </Text>
        </Stack>
      </CollapsibleSection>
      <CollapsibleSection title={CARDS[6].q}>
        <Stack gap={10}>
          <Text>{CARDS[6].a}</Text>
          <Text size="small" tone="secondary">
            Proof: <Code>{CARDS[6].proof}</Code>
          </Text>
        </Stack>
      </CollapsibleSection>
      <CollapsibleSection title={CARDS[7].q}>
        <Stack gap={10}>
          <Text>{CARDS[7].a}</Text>
          <Text size="small" tone="secondary">
            Proof: <Code>{CARDS[7].proof}</Code>
          </Text>
        </Stack>
      </CollapsibleSection>
      <CollapsibleSection title={CARDS[8].q}>
        <Stack gap={10}>
          <Text>{CARDS[8].a}</Text>
          <Text size="small" tone="secondary">
            Proof: <Code>{CARDS[8].proof}</Code>
          </Text>
        </Stack>
      </CollapsibleSection>
      <CollapsibleSection title={CARDS[9].q}>
        <Stack gap={10}>
          <Text>{CARDS[9].a}</Text>
          <Text size="small" tone="secondary">
            Proof: <Code>{CARDS[9].proof}</Code>
          </Text>
        </Stack>
      </CollapsibleSection>

      <Divider />

      <Card>
        <CardHeader>How to point at the code tomorrow</CardHeader>
        <CardBody>
          <Stack gap={8}>
            <Text>
              Start at <Code>_infra/server.py</Code> chat(), then read{" "}
              <Code>agent/pipeline.py</Code> chat_events() top to bottom, then{" "}
              <Code>agent/loop.py</Code> run_agent_turn().
            </Text>
            <Text>
              Policy index: <Code>aop_loader.py</Code> format_aop_catalog().
              The lock: <Code>tools.py</Code> PUBLIC_TOOLS. The numbers:{" "}
              <Code>agent/config.py</Code> then the comparisons in pipeline.
            </Text>
          </Stack>
        </CardBody>
      </Card>

      <Text size="small" tone="tertiary">
        Source: bookly-support-agent · study aid for Decagon-style architecture
        questions. Open the copy in Cursor canvases to quiz interactively;
        this file is the git-tracked source.
      </Text>
    </Stack>
  );
}
