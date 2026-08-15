/* ========================================================================
 * PART 03/14 - app.03-prompts.js          (grep: GENUI-SPLIT)
 * ROLE:  DEF_PROMPTS - fallback prompt templates (quick note, item chat,
 *        QA research/answer/skip/plan) merged into CFG.prompts.
 * ORDER: Part 3 - must load before app.04 (session routing) and app.10 (QA).
 * EDIT:  data.json config.prompts overrides these - edit there first when
 *        tuning agent behaviour; update the fallbacks here only to match.
 * VERIFY: syntax-check, then screenshot_genui('home','both').
 * ======================================================================== */

/* ── PROMPTS — editable in data.json → config.prompts (fallbacks inline) ── */
var DEF_PROMPTS={
  quickNote:'[QUICK NOTE - TRACKER UPDATE ONLY] You are filing a note in the Project Development Tracker. Read data.json for current projects and items. File this as a new open item (tag: feat/chore/bug/idea) under the best-fit project; if none fits, create a Miscellaneous project. Update the data bag and refresh the genui. Reply with one confirmation line. DO NOT fix, research, plan, or take any action.\nSuggested project: ${project}\nNote: ${text}\n\nAvailable projects:\n${projectContext}',
  // Friendly user-facing labels shown in the chat panel as a green notice when
  // the page sends a prompt (instead of the raw prompt as a "You" bubble).
  // Editable in data.json → config.prompts.*_userMessage / config.qa.*_userMessage.
  quickNote_userMessage:'Quick note filed to the Project Development Tracker GenUI page',
  openChat_userMessage:'Opened chat for item on the Project Development Tracker GenUI page',
  qa:{template:'[Project Readiness Agent]\nProject: ${project} (${status})\nItem: ${item} [${tag}]\nQA Status: ${qaStatus}\nUser Hint: ${hint}\n--- Q&A Thread ---\n${thread}\n${action}\n\nQUESTION RULES: Before proposing a plan, ask any clarifying questions needed — each 1 to 2 sentences max (3 ok occasionally), no preamble, no research dump in the question. Several independent questions may be asked at once (set qa.questions=[{id,text},...]); if answers chain, ask one at a time. Do NOT propose a plan while questions are pending.',
      template_userMessage:'Research request sent from the Project Development Tracker GenUI page',
      userAnswer:'--- User Answer ---\n${answer}\nACTION: User answered. If critical unknowns remain, ask the next clarifying question(s) — each 1-2 sentences; update qa.questions and keep qa.status="questions", refresh the genui, and wait. If fully specified, propose the plan now: write qa.plan (approach/files/steps/effort/impact/risks/research_notes/related_items), set qa.status="plan_ready", refresh the genui.',
      userAnswer_userMessage:'Answer sent to the Project Development Tracker GenUI page',
      skip:'ACTION: User wants to skip questions and go straight to plan — propose the plan now using reasonable assumptions.',
      skip_userMessage:'Skipped questions — plan requested from the Project Development Tracker GenUI page',
      planAction:"ACTION: User's plan decision: ${decision}",
      planAction_userMessage:'Plan decision sent to the Project Development Tracker GenUI page'}
};
CFG.prompts=CFG.prompts||{};CFG.prompts.quickNote=CFG.prompts.quickNote||DEF_PROMPTS.quickNote;
CFG.prompts.quickNote_userMessage=CFG.prompts.quickNote_userMessage||DEF_PROMPTS.quickNote_userMessage;
CFG.prompts.openChat_userMessage=CFG.prompts.openChat_userMessage||DEF_PROMPTS.openChat_userMessage;
CFG.prompts.qa=CFG.prompts.qa||{};for(var _pk in DEF_PROMPTS.qa){if(CFG.prompts.qa[_pk]===undefined)CFG.prompts.qa[_pk]=DEF_PROMPTS.qa[_pk]}

