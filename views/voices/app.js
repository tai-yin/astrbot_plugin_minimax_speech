const bridge = window.AstrBotPluginView;
const VOICE_ID = /^[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]$/;
const AUDIO_EXT = /\.(mp3|m4a|wav)$/i;

const CHIPS = [
  ["chip_female", "phrase_female"],
  ["chip_male", "phrase_male"],
  ["chip_young", "phrase_young"],
  ["chip_middle", "phrase_middle"],
  ["chip_child", "phrase_child"],
  ["chip_elder", "phrase_elder"],
  ["chip_low", "phrase_low"],
  ["chip_bright", "phrase_bright"],
  ["chip_gentle", "phrase_gentle"],
  ["chip_sweet", "phrase_sweet"],
  ["chip_magnetic", "phrase_magnetic"],
  ["chip_slow", "phrase_slow"],
  ["chip_brisk", "phrase_brisk"],
  ["chip_steady", "phrase_steady"],
  ["chip_calm", "phrase_calm"],
  ["chip_service", "phrase_service"],
  ["chip_narration", "phrase_narration"],
  ["chip_news", "phrase_news"],
  ["chip_radio", "phrase_radio"],
];
const EXAMPLES = ["example_1", "example_2", "example_3"];

let studio = null;
let locale = "en-US";
let sourceFile = null;
let sourceSeconds = null;
let promptFile = null;
let promptSeconds = null;
let busy = false;
const clips = new Map();
const player = new Audio();
let playingId = "";
let playerUrl = "";
let audioCtx = null;

function text(key, fallback) {
  if (bridge && typeof bridge.t === "function") {
    return bridge.t(`views.voices.${key}`, fallback);
  }
  return fallback;
}

function fill(template, vars) {
  return String(template).replace(/\{(\w+)\}/g, (_, name) =>
    vars[name] == null ? "" : String(vars[name]),
  );
}

function byId(id) {
  return document.getElementById(id);
}

function setText(id, value) {
  const node = byId(id);
  if (node) node.textContent = value;
}

function billedCount(value, hanWeight) {
  const source = value || "";
  if (hanWeight === 1) return [...source].length;
  let total = 0;
  for (const char of source) {
    const code = char.codePointAt(0);
    total += code >= 0x4e00 && code <= 0x9fff ? 2 : 1;
  }
  return total;
}

function money(amount, currency, approximate) {
  const symbol = currency === "CNY" ? "¥" : "$";
  const body =
    !amount
      ? `${symbol}0.00`
      : Math.abs(amount) < 0.01
        ? `${symbol}${amount.toFixed(4)}`
        : `${symbol}${amount.toFixed(2)}`;
  return approximate ? `≈ ${body}` : body;
}

function rateFor(modelId) {
  const rates = studio?.pricing?.speech_per_million || {};
  if (rates[modelId] != null) return rates[modelId];
  return String(modelId).includes("turbo") ? ratesFallback("turbo") : ratesFallback("hd");
}

function ratesFallback(tier) {
  const rates = studio?.pricing?.speech_per_million || {};
  const match = Object.entries(rates).find(([id]) =>
    tier === "turbo" ? id.includes("turbo") : !id.includes("turbo"),
  );
  return match ? match[1] : 0;
}

function estimate(value, perMillion) {
  const weight = studio?.pricing?.han_weight || 1;
  return (billedCount(value, weight) * perMillion) / 1_000_000;
}

function currency() {
  return studio?.pricing?.currency || "USD";
}

function takeById(voiceId) {
  return (studio?.takes || []).find((take) => take.voice_id === voiceId) || null;
}

function applyTheme(ctx) {
  document.documentElement.dataset.theme = ctx?.isDark ? "dark" : "light";
  locale = bridge?.getLocale?.() || ctx?.locale || locale;
}

function paintStatic() {
  document.title = text("title", "MiniMax Voice studio");
  setText("heading", text("heading", "MiniMax Voice studio"));
  setText("flow-open", text("flow_open", "How it works"));
  setText("flow-dialog-title", text("flow_dialog_title", "How this works"));
  setText("flow-close", text("flow_close", "Close"));
  setText("spend-label", text("spend", "From this page"));
  setText("tab-clone", text("tab_clone", "Clone"));
  setText("tab-design", text("tab_design", "Design"));
  setText("clone-flow-title", text("flow_clone_title", "How cloning works"));
  setText("clone-step-1", text("clone_step_1", "Upload a recording of the voice to copy."));
  setText(
    "clone-step-2",
    text("clone_step_2", "Optionally add a short clip of that voice, and type the words spoken in it."),
  );
  setText(
    "clone-step-3",
    text(
      "clone_step_3",
      "Write a preview sentence and create a draft. Playing it from the list does not charge the one-time fee.",
    ),
  );
  setText(
    "clone-step-4",
    text(
      "clone_step_4",
      "Keep the draft to save it, then use it for QQ replies. An unused draft is removed after 7 days.",
    ),
  );
  setText("design-flow-title", text("flow_design_title", "How designing works"));
  setText(
    "design-step-1",
    text("design_step_1", "Describe the voice. The phrases and examples can fill this in."),
  );
  setText(
    "design-step-2",
    text("design_step_2", "Write a preview sentence, create a draft, and listen to it in the list."),
  );
  setText(
    "design-step-3",
    text("design_step_3", "Keep the one you want. The design fee is charged when you keep it."),
  );
  setText(
    "design-step-4",
    text("design_step_4", "Use the saved voice for QQ replies. Another take starts a new voice id."),
  );
  setText("source-title", text("source", "Source audio"));
  setText("source-hint", text("source_rule", "mp3, m4a, or wav · 10 seconds to 5 minutes · up to 20 MB"));
  setText("prompt-label", text("prompt_clip", "Example clip, optional"));
  setText("prompt-hint", text("prompt_rule", "Under 8 seconds, with the words spoken in it."));
  setText("prompt-title", text("drop_short", "Add a short example"));
  setText("transcript-label", text("transcript", "Transcript of the example"));
  setText("clone-id-label", text("voice_id", "Voice id"));
  setText("clone-preview-label", text("preview", "Preview sentence"));
  setText("advanced-label", text("advanced", "Advanced"));
  setText("boost-label", text("language_boost", "Language boost"));
  setText("expected-label", text("expected_text", "Expected transcript"));
  setText("accuracy-label", text("accuracy", "Match threshold"));
  setText("noise-label", text("noise", "Reduce noise"));
  setText("volume-label", text("volume", "Normalize volume"));
  setText("verify-note", text("verify_note", "Cloning needs individual or enterprise verification on this MiniMax account."));
  setText("design-prompt-label", text("design_prompt", "Describe the voice"));
  setText("design-preview-label", text("preview", "Preview sentence"));
  setText("design-id-label", text("voice_id_optional", "Voice id, optional"));
  setText("design-id-hint", text("voice_id_optional_hint", "Leave this blank and each take gets its own id."));
  setText("shelf-label", text("shelf", "Takes"));
  setText("empty", text("empty_shelf", "Nothing here yet. A clone or a design will show up as a draft."));
  setText("audition-label", text("audition", "Try a sentence"));
  setText("audition-hint", text("audition_hint", "A draft voice asks before it charges the one-time fee."));
  setText("audition-voice-label", text("voice", "Voice"));
  setText("audition-model-label", text("model", "Model"));
  setText("audition-text-label", text("sentence", "Sentence"));
  setText("gate-title", text("no_key_title", "Add an API key"));
  setText("gate-body", text("no_key_body", "Add an API key in this plugin's settings, then reload the page."));
  setText("confirm-cancel", text("cancel", "Cancel"));
  paintChips();
  paintTips();
  paintCosts();
  paintIdHint("clone-id", "clone-id-hint");
  paintIdHint("design-id", "design-id-hint", true);
}

const TIPS = [
  ["flow-open", "tip_flow", "The steps for cloning a recording and designing a voice."],
  ["source-drop", "tip_source", "The recording MiniMax copies. mp3, m4a, or wav, from 10 seconds to 5 minutes, up to 20 MB."],
  ["prompt-drop", "tip_prompt", "Optional clip of the same person, under 8 seconds. It teaches pronunciation, and it must be sent with the transcript."],
  ["transcript-field", "tip_transcript", "The words spoken in the example clip. Fill this in only when a clip is attached."],
  ["clone-id-field", "tip_voice_id", "The name stored for this voice. 8–256 characters, starts with a letter, and cannot end with - or _. A deleted id cannot be reused."],
  ["clone-preview-field", "tip_preview", "The first line this draft speaks. Billed by the character. Creating the draft does not charge the one-time fee."],
  ["advanced", "tip_advanced", "Optional checks on the source recording. Leave these alone unless the audio needs them."],
  ["boost-field", "tip_boost", "A language for MiniMax to favor, such as Chinese or English. Leave this blank to let MiniMax decide."],
  ["expected-field", "tip_expected", "What you think the source recording says, up to 200 characters. MiniMax compares the audio with this text."],
  ["accuracy-field", "tip_accuracy", "How closely the recording must match the expected transcript, from 0 to 1. Empty uses MiniMax's default, 0.7."],
  ["noise-check", "tip_noise", "Remove background noise from the source before cloning. Leave this off unless the recording is noisy."],
  ["volume-check", "tip_volume", "Even out the loudness of the source before cloning."],
  ["clone-submit", "tip_clone_submit", "Create a draft and generate the preview. The one-time clone fee is charged only when you keep the voice."],
  ["design-prompt-field", "tip_design_prompt", "Who is speaking, and how: age, pitch, pace, and mood. Up to 2000 characters."],
  ["design-preview-field", "tip_design_preview", "The first line this draft speaks, up to 500 characters. Billed by the character. The design fee waits until you keep the voice."],
  ["design-id-field", "tip_design_id", "Leave this blank and each take gets a new id. A deleted id cannot be reused."],
  ["design-submit", "tip_design_submit", "Create a draft from the description and the preview sentence."],
  ["another", "tip_another", "Create another draft with a new voice id, using the same description."],
  ["audition-voice-field", "tip_audition_voice", "The voice that speaks the sentence. A draft asks you to confirm the one-time fee before it plays."],
  ["audition-model-field", "tip_audition_model", "The speech model for this sentence. HD costs more per character than Turbo."],
  ["audition-text-field", "tip_audition_text", "Your own sentence, up to 1000 characters. The estimate on the button is the character cost."],
  ["audition-play", "tip_audition_play", "Speak the sentence with the selected voice and model."],
];

function paintTips() {
  for (const [id, key, fallback] of TIPS) setTip(byId(id), text(key, fallback));
}

function setTip(node, message) {
  if (!node || !message) return;
  node.dataset.tip = message;
  node.setAttribute("aria-description", message);
}

function paintChips() {
  const chips = byId("chips");
  const examples = byId("examples");
  chips.replaceChildren();
  examples.replaceChildren();
  for (const [labelKey, phraseKey] of CHIPS) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text(labelKey, labelKey);
    setTip(button, text("tip_chips", "Add this phrase to the description."));
    button.addEventListener("click", () => appendPhrase(text(phraseKey, labelKey)));
    chips.appendChild(button);
  }
  for (const key of EXAMPLES) {
    const button = document.createElement("button");
    button.type = "button";
    const phrase = text(key, "");
    button.textContent = phrase.length > 48 ? `${phrase.slice(0, 48)}…` : phrase;
    setTip(button, text("tip_examples", "Replace the description with this example."));
    button.addEventListener("click", () => {
      byId("design-prompt").value = text(key, "");
      paintCosts();
    });
    examples.appendChild(button);
  }
}

function appendPhrase(phrase) {
  const box = byId("design-prompt");
  const current = box.value.trim();
  box.value = current ? `${current}, ${phrase}` : phrase;
  paintCosts();
}

function paintCosts() {
  if (!studio?.pricing) return;
  const model = studio.model;
  const cloneText = byId("clone-preview").value.trim();
  const cloneAmount = estimate(cloneText, rateFor(model));
  byId("clone-submit").textContent = cloneText
    ? `${text("submit_preview", "Preview")} ${money(cloneAmount, currency(), true)}`
    : text("submit_draft", "Create draft");
  setText(
    "clone-note",
    text("clone_fee_later", "Creating a draft does not charge the one-time voice fee."),
  );
  const designText = byId("design-preview").value.trim();
  const designAmount = estimate(
    designText,
    studio.pricing.design_preview_per_million,
  );
  byId("design-submit").textContent = designText
    ? `${text("submit_preview", "Preview")} ${money(designAmount, currency(), true)}`
    : text("submit_preview", "Preview");
  byId("another").textContent = text("another", "Another take");
  setText(
    "design-note",
    text("design_fee_later", "The design fee is charged only when you keep the voice. This preview is billed by the character."),
  );
  setText("clone-count", countLabel(cloneText.length, 1000));
  setText("design-count", countLabel(byId("design-preview").value.length, 500));
  setText("design-prompt-count", countLabel(byId("design-prompt").value.length, 2000));
  paintAuditionCost();
}

function countLabel(length, max) {
  return fill(text("counter", "{length} / {max}"), { length, max });
}

function paintIdHint(inputId, hintId, optional) {
  const value = byId(inputId).value.trim();
  const hint = byId(hintId);
  if (!value && optional) {
    hint.textContent = text("voice_id_optional_hint", "Leave this blank and each take gets its own id.");
    return;
  }
  if (!value) {
    hint.textContent = text("voice_id_hint", "8–256 characters, starts with a letter, cannot end with - or _.");
    return;
  }
  hint.textContent = VOICE_ID.test(value)
    ? text("voice_id_ok", "This id can be used.")
    : text("voice_id_bad", "8–256 characters, starts with a letter, cannot end with - or _.");
}

function paintHeader() {
  if (!studio) return;
  const rule =
    studio.pricing.han_weight === 2
      ? text("han_rule", "On the China host a Chinese character counts as 2 in these estimates.")
      : text("intl_rule", "International estimates count every character as 1. MiniMax has not published the exact rule.");
  setText(
    "eyebrow",
    `${studio.host_label} · ${studio.model}`,
  );
  byId("eyebrow").title = rule;
  const spent = Number(studio.spend || 0);
  setText("spend-value", money(spent, currency(), true));
  const billing = byId("billing");
  billing.href = studio.pricing.billing_url;
  billing.textContent = text("billing", "Billing");
  const prices = byId("prices");
  prices.href = studio.pricing.pricing_url;
  prices.textContent = fill(text("prices_checked", "List prices, {date}"), {
    date: studio.pricing.checked,
  });
  const warning = byId("warning");
  warning.hidden = !studio.warning;
  warning.textContent = studio.warning || "";
}

function paintTakes() {
  const list = byId("takes");
  const takes = studio?.takes || [];
  byId("empty").hidden = takes.length > 0;
  list.replaceChildren();
  takes.forEach((take, index) => list.appendChild(takeCard(take, index === 0)));
  for (const take of takes) {
    if (!take.has_audio) continue;
    const canvas = list.querySelector(`[data-wave="${CSS.escape(take.voice_id)}"]`);
    if (canvas) void drawWave(canvas, take.voice_id);
  }
}

function takeCard(take, latest) {
  const card = document.createElement("article");
  card.className = `take${latest ? " take-latest" : ""}${take.status === "expired" || take.status === "deleted" ? " dim" : ""}`;
  const head = document.createElement("header");
  const lamp = document.createElement("span");
  lamp.className = `lamp${take.status === "kept" ? " kept" : ""}${take.status === "expired" || take.status === "deleted" ? " dead" : ""}`;
  lamp.setAttribute("aria-hidden", "true");
  const body = document.createElement("div");
  const kind = document.createElement("p");
  kind.className = "kind";
  kind.textContent = take.kind === "design" ? text("kind_design", "Design") : text("kind_clone", "Clone");
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "copy";
  copy.textContent = take.voice_id;
  copy.addEventListener("click", () => copyId(take.voice_id, copy));
  body.append(kind, copy);
  head.append(lamp, body);
  card.append(head);
  if (take.kind === "design" && take.prompt) {
    const description = document.createElement("p");
    description.className = "description";
    const caption = document.createElement("span");
    caption.className = "description-label";
    caption.textContent = text("take_description", "Voice description");
    description.append(caption, document.createTextNode(take.prompt));
    card.append(description);
  }
  if (take.preview_text) {
    const quote = document.createElement("p");
    quote.className = "quote";
    quote.textContent = take.preview_text;
    card.append(quote);
  }
  const status = document.createElement("p");
  status.className = "status";
  status.textContent = statusLabel(take);
  card.append(status);
  if (take.has_audio) {
    const canvas = document.createElement("canvas");
    canvas.className = "wave";
    canvas.dataset.wave = take.voice_id;
    card.append(canvas);
    const play = button(
      playingId === take.voice_id ? text("pause", "Pause") : text("play", "Play"),
      latest ? "primary" : "quiet",
      () => playTake(take),
    );
    play.dataset.play = take.voice_id;
    setTip(play, text("tip_play", "Play the preview stored for this voice. This does not charge the one-time fee."));
    card.append(actionRow(take, play));
  } else {
    card.append(actionRow(take, null));
  }
  return card;
}

function actionRow(take, play) {
  const row = document.createElement("div");
  row.className = "take-actions";
  if (play) row.append(play);
  if (take.status === "draft") {
    row.append(
      button(
        keepLabel(take),
        "quiet",
        () => keepTake(take),
        text("tip_keep", "Save this draft on the account. This charges the one-time fee plus one short sentence."),
      ),
    );
  }
  if (take.status === "draft" || take.status === "kept") {
    row.append(
      button(
        text("try", "Try a sentence"),
        "quiet",
        () => armAudition(take),
        text("tip_try", "Type your own sentence for this voice in the bar below."),
      ),
    );
  }
  if (take.status === "kept") {
    const used = studio.reply_voice_id === take.voice_id;
    const use = button(
      used ? text("in_use", "Used for replies") : text("use", "Use for replies"),
      "quiet",
      () => useTake(take),
      text("tip_use", "Make QQ replies use this voice. A draft has to be kept first."),
    );
    use.disabled = used;
    row.append(use);
  }
  const gone = take.status === "expired" || take.status === "deleted";
  const draft = take.status === "draft";
  row.append(
    button(
      gone || draft ? text("remove", "Remove") : text("delete", "Delete"),
      gone || draft ? "quiet" : "danger",
      () => deleteTake(take),
      draft
        ? text(
            "tip_drop_draft",
            "Take this unsaved preview off the page. MiniMax drops it on its own after 7 days, and the id stays reserved until then.",
          )
        : gone
          ? text("tip_remove", "Drop this row from the list. MiniMax is not called again.")
          : text("tip_delete", "Delete this voice at MiniMax. The id cannot be used again."),
    ),
  );
  return row;
}

function button(label, className, onClick, tip) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = className;
  node.textContent = label;
  node.addEventListener("click", onClick);
  setTip(node, tip);
  return node;
}

function statusLabel(take) {
  if (take.status === "draft") {
    return fill(text("draft_status", "Not charged yet. Deleted by MiniMax in {days} days."), {
      days: take.days_left ?? "",
    });
  }
  if (take.status === "kept" && take.fee_label) {
    return fill(text("kept_fee", "Saved · {fee}"), { fee: take.fee_label });
  }
  if (take.status === "kept") return text("kept_status", "Saved on this account.");
  if (take.status === "expired") return text("expired_status", "Expired. MiniMax deleted this draft.");
  return text("deleted_status", "Deleted. This voice id cannot be reused.");
}

function keepLabel(take) {
  const fee = take.kind === "design" ? studio.pricing.design_fee : studio.pricing.clone_fee;
  const extra = estimate(studio.keep_text || "", rateFor(studio.model));
  return `${text("keep", "Keep")} · ${money(fee, currency(), false)} + ${money(extra, currency(), true)}`;
}

function paintAudition() {
  const voice = byId("audition-voice");
  const previous = voice.value;
  voice.replaceChildren();
  const groups = [
    [text("shelf", "Takes"), studio.takes || []],
    [text("system_voices", "System voices"), studio.voices || []],
  ];
  for (const [label, items] of groups) {
    if (!items.length) continue;
    const group = document.createElement("optgroup");
    group.label = label;
    for (const item of items) {
      const option = document.createElement("option");
      option.value = item.voice_id;
      option.textContent = item.label && item.label !== item.voice_id
        ? `${item.label} · ${item.voice_id}`
        : item.voice_id;
      group.append(option);
    }
    voice.append(group);
  }
  if (previous) voice.value = previous;
  const model = byId("audition-model");
  if (!model.options.length) {
    for (const item of studio.models || []) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.label;
      model.append(option);
    }
    model.value = studio.model;
  }
  paintAuditionCost();
}

function paintAuditionCost() {
  if (!studio?.pricing) return;
  const sentence = byId("audition-text").value.trim();
  const selected = takeById(byId("audition-voice").value);
  const amount = estimate(sentence, rateFor(byId("audition-model").value || studio.model));
  const play = byId("audition-play");
  if (selected?.status === "draft") {
    const fee = selected.kind === "design" ? studio.pricing.design_fee : studio.pricing.clone_fee;
    play.textContent = `${text("play", "Play")} · ${money(fee, currency(), false)} + ${money(amount, currency(), true)}`;
  } else {
    play.textContent = sentence
      ? `${text("play", "Play")} · ${money(amount, currency(), true)}`
      : text("play", "Play");
  }
}

async function load() {
  studio = await bridge.apiGet("state", { locale });
  const open = Boolean(studio.has_key);
  byId("gate").hidden = open;
  byId("workspace").hidden = !open;
  byId("audition").hidden = !open;
  byId("spend").hidden = !open;
  if (!byId("clone-id").value && studio.suggested_clone_id) {
    byId("clone-id").value = studio.suggested_clone_id;
  }
  paintStatic();
  paintHeader();
  if (!open) return;
  paintTakes();
  paintAudition();
}

function stage(message, tone) {
  const node = byId("stage");
  node.textContent = message || "";
  node.classList.toggle("error", tone === "error");
  if (!message) return;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  node.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
}

function fail(error) {
  stage(error?.message || text("failed", "Something went wrong."), "error");
}

function setBusy(next) {
  busy = next;
  for (const id of ["clone-submit", "design-submit", "another", "audition-play"]) {
    byId(id).disabled = next;
  }
}

async function readDuration(file) {
  const url = URL.createObjectURL(file);
  const audio = document.createElement("audio");
  audio.preload = "metadata";
  try {
    const duration = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("timeout")), 8000);
      audio.onloadedmetadata = () => {
        clearTimeout(timer);
        resolve(audio.duration);
      };
      audio.onerror = () => {
        clearTimeout(timer);
        reject(new Error("unreadable"));
      };
      audio.src = url;
    });
    if (!Number.isFinite(duration)) throw new Error("unreadable");
    return duration;
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function acceptFile(file, kind) {
  if (!file) return;
  if (!AUDIO_EXT.test(file.name || "")) {
    throw new Error(text("err_type", "Use an mp3, m4a, or wav file."));
  }
  if (file.size > 20 * 1024 * 1024) {
    throw new Error(text("err_size", "The audio file is larger than 20 MB."));
  }
  const seconds = await readDuration(file);
  if (kind === "source" && (seconds < 10 || seconds > 300)) {
    throw new Error(text("err_duration_source", "Source audio must be between 10 seconds and 5 minutes."));
  }
  if (kind === "prompt" && !(seconds > 0 && seconds < 8)) {
    throw new Error(text("err_duration_prompt", "The example clip must be shorter than 8 seconds."));
  }
  const label = `${file.name} · ${seconds.toFixed(1)}s`;
  if (kind === "source") {
    sourceFile = file;
    sourceSeconds = seconds;
    setText("source-name", label);
  } else {
    promptFile = file;
    promptSeconds = seconds;
    setText("prompt-name", label);
  }
}

function watchDrop(zoneId, inputId, kind) {
  const zone = byId(zoneId);
  const input = byId(inputId);
  input.addEventListener("change", () => {
    stage("");
    acceptFile(input.files?.[0], kind).catch(fail);
  });
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("over");
    stage("");
    acceptFile(event.dataTransfer?.files?.[0], kind).catch(fail);
  });
}

async function submitClone() {
  if (busy) return;
  stage("");
  if (!sourceFile) {
    fail(new Error(text("err_source", "Choose the source audio.")));
    return;
  }
  const transcript = byId("transcript").value.trim();
  if (Boolean(promptFile) !== Boolean(transcript)) {
    fail(new Error(text("err_prompt_pair", "The example clip and its transcript must be sent together.")));
    return;
  }
  const voiceId = byId("clone-id").value.trim();
  if (!VOICE_ID.test(voiceId)) {
    fail(new Error(text("voice_id_bad", "Check the voice id.")));
    return;
  }
  setBusy(true);
  try {
    stage(text("stage_upload", "Uploading audio…"));
    const source = await bridge.upload("upload/source", sourceFile);
    let promptId = "";
    if (promptFile) {
      const prompt = await bridge.upload("upload/prompt", promptFile);
      promptId = prompt.upload_id;
    }
    stage(text("stage_clone", "Cloning…"));
    const result = await bridge.apiPost("clone", {
      locale,
      source_id: source.upload_id,
      prompt_id: promptId,
      prompt_text: transcript,
      voice_id: voiceId,
      text: byId("clone-preview").value.trim(),
      source_seconds: sourceSeconds,
      prompt_seconds: promptSeconds,
      language_boost: byId("boost").value.trim(),
      text_validation: byId("expected").value.trim(),
      accuracy: byId("accuracy").value,
      need_noise_reduction: byId("noise").checked,
      need_volume_normalization: byId("volume").checked,
    });
    stage(text("stage_preview", "Preview ready."));
    remember(result.take?.voice_id, result.audio_base64, result.media_type);
    byId("clone-id").value = "";
    await load();
  } catch (error) {
    fail(error);
  } finally {
    setBusy(false);
  }
}

async function submitDesign(another) {
  if (busy) return;
  stage("");
  const prompt = byId("design-prompt").value.trim();
  const preview = byId("design-preview").value.trim();
  if (!prompt) {
    fail(new Error(text("err_design_prompt", "Describe the voice.")));
    return;
  }
  if (!preview) {
    fail(new Error(text("err_preview", "Preview text is required.")));
    return;
  }
  const voiceId = another ? "" : byId("design-id").value.trim();
  if (voiceId && !VOICE_ID.test(voiceId)) {
    fail(new Error(text("voice_id_bad", "Check the voice id.")));
    return;
  }
  setBusy(true);
  try {
    stage(text("stage_design", "Designing…"));
    const result = await bridge.apiPost("design", {
      locale,
      prompt,
      preview_text: preview,
      voice_id: voiceId,
    });
    stage(text("stage_preview", "Preview ready."));
    remember(result.take?.voice_id, result.audio_base64, result.media_type);
    if (!another) byId("design-id").value = "";
    await load();
  } catch (error) {
    fail(error);
  } finally {
    setBusy(false);
  }
}

function remember(voiceId, encoded, mediaType) {
  if (!voiceId || !encoded) return;
  const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
  clips.set(voiceId, new Blob([bytes], { type: mediaType || "audio/mpeg" }));
}

async function clipFor(take) {
  if (clips.has(take.voice_id)) return clips.get(take.voice_id);
  const payload = await bridge.apiGet(`takes/${encodeURIComponent(take.voice_id)}/audio`, { locale });
  remember(take.voice_id, payload.audio_base64, payload.media_type);
  return clips.get(take.voice_id);
}

async function playTake(take) {
  try {
    if (playingId === take.voice_id) {
      stopPlayer();
      paintPlayState();
      return;
    }
    const blob = await clipFor(take);
    playBlob(take.voice_id, blob);
  } catch (error) {
    fail(error);
  }
}

function playBlob(id, blob) {
  if (playerUrl) URL.revokeObjectURL(playerUrl);
  playerUrl = URL.createObjectURL(blob);
  player.src = playerUrl;
  playingId = id;
  void player.play();
  paintPlayState();
}

function stopPlayer() {
  player.pause();
  playingId = "";
}

function paintPlayState() {
  for (const node of document.querySelectorAll("[data-play]")) {
    const active = node.dataset.play === playingId;
    node.textContent = active ? text("pause", "Pause") : text("play", "Play");
  }
}

async function drawWave(canvas, voiceId) {
  try {
    const blob = clips.get(voiceId) || (await clipFor({ voice_id: voiceId }));
    if (!blob) return;
    if (!audioCtx) audioCtx = new AudioContext();
    const decoded = await audioCtx.decodeAudioData((await blob.arrayBuffer()).slice(0));
    const channel = decoded.getChannelData(0);
    const bars = 56;
    const width = canvas.clientWidth || 320;
    const height = canvas.clientHeight || 48;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.floor(width * ratio);
    canvas.height = Math.floor(height * ratio);
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    ctx.clearRect(0, 0, width, height);
    const style = getComputedStyle(document.documentElement);
    ctx.fillStyle = style.getPropertyValue("--lamp").trim() || "#c9842a";
    const step = Math.max(1, Math.floor(channel.length / bars));
    const gap = 2;
    const barWidth = Math.max(2, (width - gap * bars) / bars);
    for (let index = 0; index < bars; index += 1) {
      let peak = 0;
      const start = index * step;
      for (let sample = start; sample < start + step && sample < channel.length; sample += 1) {
        peak = Math.max(peak, Math.abs(channel[sample]));
      }
      const barHeight = Math.max(2, peak * (height - 4));
      const x = index * (barWidth + gap);
      const y = (height - barHeight) / 2;
      if (typeof ctx.roundRect === "function") {
        ctx.beginPath();
        ctx.roundRect(x, y, barWidth, barHeight, 2);
        ctx.fill();
      } else {
        ctx.fillRect(x, y, barWidth, barHeight);
      }
    }
  } catch {
    /* A preview that cannot be decoded still has a play button. */
  }
}

async function ask(title, body, ok, danger) {
  byId("confirm-title").textContent = title;
  byId("confirm-body").textContent = body;
  const confirmOk = byId("confirm-ok");
  confirmOk.textContent = ok;
  confirmOk.classList.add("primary");
  confirmOk.classList.toggle("danger", Boolean(danger));
  const dialog = byId("confirm");
  if (dialog.open) dialog.close();
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
}

async function keepTake(take) {
  const accepted = await ask(
    text("confirm_keep_title", "Keep this voice?"),
    fill(text("confirm_keep_body", "MiniMax charges {cost} to save this voice."), {
      cost: keepLabel(take).split(" · ").slice(1).join(" · "),
    }),
    text("keep", "Keep"),
    false,
  );
  if (!accepted) return;
  setBusy(true);
  try {
    const result = await bridge.apiPost("keep", {
      locale,
      voice_id: take.voice_id,
      confirm_fee: true,
    });
    remember(`${take.voice_id}:keep`, result.audio_base64, result.media_type);
    if (result.audio_base64) {
      const bytes = Uint8Array.from(atob(result.audio_base64), (char) => char.charCodeAt(0));
      playBlob(`${take.voice_id}:keep`, new Blob([bytes], { type: result.media_type || "audio/mpeg" }));
    }
    await load();
    stage(text("kept_done", "Saved. The one-time fee has been charged."));
  } catch (error) {
    fail(error);
  } finally {
    setBusy(false);
  }
}

function armAudition(take) {
  byId("audition-voice").value = take.voice_id;
  paintAuditionCost();
  byId("audition-text").focus();
}

async function useTake(take) {
  setBusy(true);
  try {
    await bridge.apiPost("use", { locale, voice_id: take.voice_id });
    await load();
    stage(text("use_done", "QQ replies will use this voice."));
  } catch (error) {
    fail(error);
  } finally {
    setBusy(false);
  }
}

async function deleteTake(take) {
  const gone = take.status === "expired" || take.status === "deleted";
  const draft = take.status === "draft";
  const accepted = await ask(
    draft
      ? text("confirm_drop_title", "Remove this preview?")
      : gone
        ? text("confirm_remove_title", "Remove this from the list?")
        : text("confirm_delete_title", "Delete this voice?"),
    draft
      ? text(
          "confirm_drop_body",
          "MiniMax cannot delete an unsaved preview. It expires on its own, and the voice id stays reserved until then. This only removes it from the page.",
        )
      : gone
        ? text("confirm_remove_body", "This only removes it from the page.")
        : text("confirm_delete_body", "MiniMax will not let you use this voice id again."),
    draft || gone ? text("remove", "Remove") : text("delete", "Delete"),
    !draft && !gone,
  );
  if (!accepted) return;
  setBusy(true);
  try {
    const result = await bridge.apiPost("delete", {
      locale,
      voice_id: take.voice_id,
      kind: take.kind,
    });
    clips.delete(take.voice_id);
    await load();
    if (result?.preview_only) {
      stage(
        text(
          "preview_removed",
          "Removed from this page. MiniMax drops an unsaved preview on its own.",
        ),
      );
    }
  } catch (error) {
    fail(error);
  } finally {
    setBusy(false);
  }
}

async function submitAudition(event) {
  event.preventDefault();
  if (busy) return;
  const voiceId = byId("audition-voice").value;
  const sentence = byId("audition-text").value.trim();
  if (!voiceId || !sentence) return;
  const take = takeById(voiceId);
  let confirmFee = false;
  if (take?.status === "draft") {
    const fee = take.kind === "design" ? studio.pricing.design_fee : studio.pricing.clone_fee;
    const accepted = await ask(
      text("confirm_try_title", "This draft will be saved"),
      fill(text("confirm_try_body", "Playing it charges the {fee} one-time fee, plus the sentence."), {
        fee: money(fee, currency(), false),
      }),
      text("play", "Play"),
      false,
    );
    if (!accepted) return;
    confirmFee = true;
  }
  setBusy(true);
  try {
    const result = await bridge.apiPost("audition", {
      locale,
      voice_id: voiceId,
      text: sentence,
      model: byId("audition-model").value,
      confirm_fee: confirmFee,
    });
    const bytes = Uint8Array.from(atob(result.audio_base64), (char) => char.charCodeAt(0));
    playBlob("audition", new Blob([bytes], { type: result.media_type || "audio/mpeg" }));
    if (result.kept) await load();
    else if (typeof result.spend === "number") {
      studio.spend = result.spend;
      paintHeader();
    }
  } catch (error) {
    fail(error);
  } finally {
    setBusy(false);
  }
}

async function copyId(voiceId, node) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(voiceId);
    else throw new Error("clipboard");
    node.textContent = text("copied", "Copied");
    setTimeout(() => {
      node.textContent = voiceId;
    }, 1200);
  } catch {
    node.textContent = voiceId;
  }
}

function selectTab(name) {
  const clone = name === "clone";
  byId("tab-clone").setAttribute("aria-selected", String(clone));
  byId("tab-design").setAttribute("aria-selected", String(!clone));
  byId("panel-clone").hidden = !clone;
  byId("panel-design").hidden = clone;
}

player.addEventListener("ended", () => {
  playingId = "";
  paintPlayState();
});

function showFlow() {
  const dialog = byId("flow");
  if (!dialog || dialog.open || byId("confirm").open) return;
  dialog.showModal();
}

byId("flow-open").addEventListener("click", showFlow);
byId("tab-clone").addEventListener("click", () => selectTab("clone"));
byId("tab-design").addEventListener("click", () => selectTab("design"));
byId("clone-submit").addEventListener("click", () => void submitClone());
byId("design-submit").addEventListener("click", () => void submitDesign(false));
byId("another").addEventListener("click", () => void submitDesign(true));
byId("audition").addEventListener("submit", (event) => void submitAudition(event));
for (const id of ["clone-preview", "design-preview", "design-prompt", "audition-text"]) {
  byId(id).addEventListener("input", paintCosts);
}
byId("audition-voice").addEventListener("change", paintAuditionCost);
byId("audition-model").addEventListener("change", paintAuditionCost);
byId("clone-id").addEventListener("input", () => paintIdHint("clone-id", "clone-id-hint"));
byId("design-id").addEventListener("input", () => paintIdHint("design-id", "design-id-hint", true));
watchDrop("source-drop", "source-file", "source");
watchDrop("prompt-drop", "prompt-file", "prompt");

function bindHoverTip() {
  const bubble = document.createElement("p");
  bubble.className = "hover-tip";
  bubble.hidden = true;
  document.body.append(bubble);
  let host = null;
  let pending = null;
  let timer = 0;

  function hide() {
    window.clearTimeout(timer);
    host = null;
    pending = null;
    bubble.hidden = true;
  }

  function place(target) {
    const message = target?.dataset?.tip;
    if (!message) {
      hide();
      return;
    }
    host = target;
    bubble.textContent = message;
    bubble.hidden = false;
    const rect = target.getBoundingClientRect();
    const margin = 8;
    const roomRight = window.innerWidth - rect.right - margin * 2;
    const roomLeft = rect.left - margin * 2;
    const width = Math.min(280, Math.max(roomRight, roomLeft, 160));
    bubble.style.maxWidth = `${width}px`;
    const box = bubble.getBoundingClientRect();
    let left = rect.right + margin;
    if (left + box.width > window.innerWidth - margin) left = rect.left - box.width - margin;
    let top = rect.top + (rect.height - box.height) / 2;
    if (top + box.height > window.innerHeight - margin) top = window.innerHeight - box.height - margin;
    bubble.style.top = `${Math.max(margin, top)}px`;
    bubble.style.left = `${Math.max(margin, left)}px`;
  }

  document.addEventListener("mouseover", (event) => {
    const next = event.target.closest?.("[data-tip]");
    if (!next) {
      hide();
      return;
    }
    if (next === host || next === pending) return;
    window.clearTimeout(timer);
    pending = next;
    timer = window.setTimeout(() => {
      pending = null;
      place(next);
    }, 280);
  });
  document.addEventListener("mouseout", (event) => {
    const current = event.target.closest?.("[data-tip]");
    if (!current) return;
    const next = event.relatedTarget?.closest?.("[data-tip]");
    if (next === current) return;
    if (current === pending) {
      window.clearTimeout(timer);
      pending = null;
    }
    if (current === host) hide();
  });
  document.addEventListener("focusin", (event) => {
    const next = event.target.closest?.("[data-tip]");
    window.clearTimeout(timer);
    if (next) place(next);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", hide, true);
}

bindHoverTip();

async function openStudio() {
  paintStatic();
  if (!bridge) {
    byId("gate").hidden = false;
    byId("gate-body").textContent = text(
      "no_bridge",
      "Open this page from the plugin detail screen.",
    );
    return;
  }
  await bridge.ready();
  applyTheme(bridge.getContext());
  bridge.onContext((ctx) => {
    applyTheme(ctx);
    paintStatic();
    if (studio) {
      paintHeader();
      paintTakes();
      paintAuditionCost();
    }
  });
  try {
    await load();
  } catch (error) {
    byId("gate").hidden = false;
    byId("gate-title").textContent = text("failed", "Something went wrong.");
    byId("gate-body").textContent = error?.message || "";
  }
}

openStudio();
