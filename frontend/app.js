/* LAMC Chatbot – frontend logic */

const messages = document.getElementById("messages");
const form = document.getElementById("chat-form");
const input = document.getElementById("message");
const sendBtn = document.getElementById("send-btn");
const healthDot = document.getElementById("health-dot");
const typingIndicator = document.getElementById("typing-indicator");
const srAnnouncements = document.getElementById("sr-announcements");

// ── Helpers ───────────────────────────────────────

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// Convert markdown inline formatting (bold, italic, links) in already-escaped HTML
function inlineMarkdown(html) {
  return html
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")   // **bold**
    .replace(/\*(.+?)\*/g, "<em>$1</em>")                // *italic*
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,      // [text](url)
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

// ── Response parser: turns plain text into structured blocks ──

function parseBlocks(text) {
  var escaped = escapeHtml(text);
  var lines = escaped.split("\n");
  var blocks = [];
  var current = null;

  function flush() {
    if (current) { blocks.push(current); current = null; }
  }

  for (var i = 0; i < lines.length; i++) {
    var raw = lines[i];
    var trimmed = raw.trim();

    // Blank line → flush current block
    if (trimmed === "") { flush(); continue; }

    // Numbered step:  "1. text" or "1.  **Label**: text"
    var numMatch = trimmed.match(/^(\d+)\.\s+(.+)/);
    if (numMatch) {
      // If we're not already in a numbered-list block, start one
      if (!current || current.type !== "ol") { flush(); current = { type: "ol", items: [] }; }
      current.items.push(numMatch[2]);
      continue;
    }

    // Bullet line:  "- text" or "* text" or "• text"
    if (/^[-•*]\s+/.test(trimmed)) {
      if (!current || current.type !== "ul") { flush(); current = { type: "ul", items: [] }; }
      current.items.push(trimmed.replace(/^[-•*]\s+/, ""));
      continue;
    }

    // Header-like line: short text ending with ":" (and no period before colon)
    if (/^[^.]{4,80}:$/.test(trimmed)) {
      flush();
      blocks.push({ type: "header", text: trimmed.replace(/:$/, "") });
      continue;
    }

    // Regular paragraph line — append to current paragraph block
    if (!current || current.type !== "p") { flush(); current = { type: "p", lines: [] }; }
    current.lines.push(trimmed);
  }
  flush();
  return blocks;
}

// ── Smart section grouping: pairs headers with the list/content that follows ──

function groupSections(blocks) {
  var sections = [];

  for (var i = 0; i < blocks.length; i++) {
    var b = blocks[i];
    // If this is a header and next block is a list, merge them
    if (b.type === "header" && i + 1 < blocks.length &&
        (blocks[i + 1].type === "ul" || blocks[i + 1].type === "ol")) {
      sections.push({ type: "section", title: b.text, content: blocks[i + 1] });
      i++; // skip the list block
    } else {
      sections.push(b);
    }
  }
  return sections;
}

// ── Render a list block to HTML ──

function renderList(block) {
  var tag = block.type === "ol" ? "ol" : "ul";
  var html = "<" + tag + ">";
  for (var j = 0; j < block.items.length; j++) {
    html += "<li>" + inlineMarkdown(block.items[j]) + "</li>";
  }
  html += "</" + tag + ">";
  return html;
}

// ── Render a block to HTML ──

function renderBlock(block) {
  if (block.type === "p") return "<p>" + inlineMarkdown(block.lines.join("<br>")) + "</p>";
  if (block.type === "ul" || block.type === "ol") return renderList(block);
  if (block.type === "header") return "<p><strong>" + inlineMarkdown(block.text) + "</strong></p>";
  if (block.type === "section") {
    return '<details class="resp-accordion">' +
      "<summary>" + inlineMarkdown(block.title) + "</summary>" +
      '<div class="resp-body">' + renderBlock(block.content) + "</div>" +
      "</details>";
  }
  return "";
}

// ── Main formatter: decides layout based on response length/structure ──

var SHORT_THRESHOLD = 300;

function formatAssistantText(text) {
  // Short responses: simple render, no accordions
  if (text.length < SHORT_THRESHOLD) {
    return formatSimple(text);
  }

  var blocks = parseBlocks(text);
  var sections = groupSections(blocks);

  // If very few sections, also just render simply
  if (sections.length <= 2) {
    var html = "";
    for (var i = 0; i < sections.length; i++) html += renderBlock(sections[i]);
    return html || "<p>" + escapeHtml(text) + "</p>";
  }

  // Build progressive disclosure:
  // - First block always visible as summary
  // - Remaining blocks: collapsible sections get their own accordion,
  //   consecutive non-section blocks get grouped into a "More details" accordion
  var visible = renderBlock(sections[0]);
  var rest = "";
  var pendingParagraphs = "";

  for (var i = 1; i < sections.length; i++) {
    var s = sections[i];
    if (s.type === "section") {
      // Flush any pending paragraphs into a "More details" accordion
      if (pendingParagraphs) {
        rest += makeAccordion("More details", pendingParagraphs);
        pendingParagraphs = "";
      }
      rest += renderBlock(s);
    } else {
      pendingParagraphs += renderBlock(s);
    }
  }

  // Remaining non-section content
  if (pendingParagraphs) {
    // If there were already accordions, wrap remaining in one too
    if (rest) {
      rest += makeAccordion("More details", pendingParagraphs);
    } else {
      // No accordions yet — make one "Read more" for everything after the first block
      rest = makeAccordion("Read more", pendingParagraphs);
    }
  }

  return visible + rest;
}

function makeAccordion(title, innerHtml) {
  return '<details class="resp-accordion">' +
    "<summary>" + escapeHtml(title) + "</summary>" +
    '<div class="resp-body">' + innerHtml + "</div>" +
    "</details>";
}

// Fallback simple formatter for short texts
function formatSimple(text) {
  var escaped = escapeHtml(text);
  var lines = escaped.split("\n");
  var html = "";
  var inList = false;

  for (var i = 0; i < lines.length; i++) {
    var trimmed = lines[i].trim();
    if (/^[-•*]\s+/.test(trimmed)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += "<li>" + inlineMarkdown(trimmed.replace(/^[-•*]\s+/, "")) + "</li>";
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      if (trimmed === "") continue;
      html += "<p>" + inlineMarkdown(trimmed) + "</p>";
    }
  }
  if (inList) html += "</ul>";
  return html || "<p>" + inlineMarkdown(escaped) + "</p>";
}

function isNearBottom(el, threshold) {
  threshold = threshold || 150;
  return el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
}

function announce(text) {
  srAnnouncements.textContent = "";
  requestAnimationFrame(function () {
    srAnnouncements.textContent = text;
  });
}

// ── Clarifying question detection & option chips ──

function isClarifyingResponse(text) {
  return /please (clarify|pick|choose|specify|select)/i.test(text) ||
    /I found multiple/i.test(text);
}

function extractOptionName(itemText) {
  // "Art History (AA)" → "Art History"
  var match = itemText.match(/^(.+?)\s*\(/);
  return match ? match[1].trim() : itemText.trim();
}

function addOptionChips(bubble, rawText) {
  if (!isClarifyingResponse(rawText)) return;
  var listItems = bubble.querySelectorAll("li");
  if (listItems.length === 0) return;

  // Detect if this is a course sequence disambiguation
  var isSequence = /course sequence/i.test(rawText);
  var prefix = isSequence ? "Show me the course sequence for " : "Tell me about ";

  var list = listItems[0].parentElement;
  var chipContainer = document.createElement("div");
  chipContainer.className = "option-chips";
  chipContainer.setAttribute("role", "group");
  chipContainer.setAttribute("aria-label", "Choose an option");

  for (var i = 0; i < listItems.length; i++) {
    var optionText = listItems[i].textContent.trim();
    var optionName = extractOptionName(optionText);

    var chip = document.createElement("button");
    chip.type = "button";
    chip.className = "option-chip";
    chip.textContent = optionText;
    // For sequence disambiguation, use full name (with award type) to avoid re-disambiguation
    var clickName = isSequence ? optionText : optionName;
    chip.addEventListener("click", (function (name, pfx) {
      return function () {
        input.value = pfx + name;
        autoGrow();
        form.requestSubmit();
      };
    })(clickName, prefix));
    chipContainer.appendChild(chip);
  }

  list.parentNode.replaceChild(chipContainer, list);
}

// ── Message rendering ─────────────────────────────

function appendMessage(role, text, forceScroll) {
  var shouldScroll = forceScroll || isNearBottom(messages);

  var article = document.createElement("article");
  article.className = "message " + role;
  // System notices (fallback-mode / rate-limit) are important status changes.
  if (role === "system") {
    article.setAttribute("role", "alert");
  }

  var bubble = document.createElement("div");
  bubble.className = "bubble";

  if (role === "user") {
    bubble.textContent = text;
  } else {
    bubble.innerHTML = formatAssistantText(text);
    addOptionChips(bubble, text);
  }

  article.appendChild(bubble);

  // Copy button for assistant messages (hover-revealed)
  if (role === "assistant") {
    var copyBtn = document.createElement("button");
    copyBtn.className = "copy-btn";
    copyBtn.type = "button";
    copyBtn.setAttribute("aria-label", "Copy response to clipboard");
    copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg><span>Copy</span>';
    copyBtn.addEventListener("click", function () {
      navigator.clipboard.writeText(text).then(function () {
        copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg><span>Copied</span>';
        copyBtn.classList.add("copied");
        setTimeout(function () {
          copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg><span>Copy</span>';
          copyBtn.classList.remove("copied");
        }, 2000);
      });
    });
    article.appendChild(copyBtn);
  }
  messages.appendChild(article);

  if (shouldScroll) {
    messages.scrollTop = messages.scrollHeight;
  }
}

function showWelcome() {
  var article = document.createElement("article");
  article.className = "message assistant";
  var bubble = document.createElement("div");
  bubble.className = "bubble welcome-bubble";
  bubble.innerHTML =
    "<p><strong>Welcome!</strong> I can help with:</p>" +
    "<ul>" +
    "<li>Class schedules and section availability</li>" +
    "<li>Degree and certificate program info</li>" +
    "<li>Counseling and student services</li>" +
    "<li>Campus info, enrollment, and transfers</li>" +
    "</ul>" +
    "<p>Type a question or click a sample on the left.</p>";
  article.appendChild(bubble);
  messages.appendChild(article);
}

// ── Typing indicator ──────────────────────────────

function showTyping() {
  typingIndicator.hidden = false;
  typingIndicator.setAttribute("aria-hidden", "false");
  announce("Assistant is typing");
  messages.scrollTop = messages.scrollHeight;
}

function hideTyping() {
  typingIndicator.hidden = true;
  typingIndicator.setAttribute("aria-hidden", "true");
}

// ── Loading state ─────────────────────────────────

function setLoading(on) {
  sendBtn.disabled = on;
  input.disabled = on;
  if (on) showTyping();
  else hideTyping();
}

// ── Auto-grow textarea ────────────────────────────

function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 150) + "px";
}

input.addEventListener("input", autoGrow);

// ── Enter to send ─────────────────────────────────

input.addEventListener("keydown", function (e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

// ── Conversation history ──────────────────────────

// The clarifying question the server is currently waiting on (or null). Set from
// ChatResponse.pending, echoed back on the next turn as ChatRequest.pending.
var pendingClarification = null;

function getHistory() {
  var articles = document.querySelectorAll("#messages article.message");
  var history = [];
  for (var i = 0; i < articles.length; i++) {
    var el = articles[i];
    var role = el.classList.contains("user") ? "user" : "assistant";
    var bubble = el.querySelector(".bubble");
    if (!bubble) continue;
    var limit = (role === "assistant") ? 1500 : 300;
    var text = bubble.textContent.trim().substring(0, limit);
    if (text) history.push({ role: role, content: text });
  }
  // Deep enough that a clarify-back exchange (ask → reply → ask → reply) can't
  // push the original question out of the server's rewrite window.
  return history.slice(-10);
}

// ── Form submit ───────────────────────────────────

form.addEventListener("submit", function (e) {
  e.preventDefault();
  var text = input.value.trim();
  if (!text || sendBtn.disabled) return;

  // Snapshot history BEFORE rendering this turn's bubble — otherwise the current
  // message lands in `history` as well as `message`, and the duplicate pushes the
  // oldest turn out of the server's rewrite window. In a clarify-back exchange
  // that dropped the original question, so the rewriter reconstructed the user's
  // terse reply as the bot's OWN clarifying question and sent that to RAG.
  var history = getHistory();

  appendMessage("user", text, true);
  input.value = "";
  input.style.height = "auto";
  setLoading(true);

  // The clarify-back we're answering, if any. The server sent it with its last
  // reply; echoing it back lets the server fill the slot deterministically
  // instead of re-deriving what we were talking about from the transcript.
  var pendingToSend = pendingClarification;

  fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message: text,
      history: history,
      campus: document.body.dataset.campusCode || null,
      pending: pendingToSend,
    }),
  })
    .then(function (response) { return response.json(); })
    .then(function (data) {
      // Carry a new clarification forward; clear it once we get a real answer.
      pendingClarification = data.pending || null;

      if (data.fallback_mode) {
        appendMessage("system", "⚠️ The chatbot has switched to free open-source models. Chat quality may be reduced — live schedules, programs, and campus-specific information are not available in this mode.");
      }
      if (data.rate_limited) {
        appendMessage("system", "🚫 Daily usage limit reached. Please come back tomorrow when the limit resets.");
      }
      appendMessage("assistant", data.answer);
      announce("New response from assistant");
    })
    .catch(function (error) {
      appendMessage("assistant", "Request failed: " + error.message);
      announce("Error receiving response");
    })
    .finally(function () {
      setLoading(false);
      input.focus();
    });
});

// ── Question button clicks ────────────────────────

document.querySelectorAll(".q-btn").forEach(function (btn) {
  btn.addEventListener("click", function () {
    var question = btn.getAttribute("data-question");
    if (question) {
      input.value = question;
      input.focus();
      autoGrow();
    }
  });
});

// ── Health check ──────────────────────────────────

function fetchHealth() {
  fetch("/api/health")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var status = (data.status || "").toLowerCase();
      var label;
      if (status === "healthy" || status === "ok") {
        healthDot.className = "health-dot ok";
        label = "Backend healthy";
      } else if (status === "degraded") {
        healthDot.className = "health-dot degraded";
        label = "Backend degraded";
      } else {
        healthDot.className = "health-dot down";
        label = "Backend status: " + data.status;
      }
      // Expose status to assistive tech, not just via color/title (WCAG 1.4.1).
      healthDot.title = label;
      healthDot.setAttribute("aria-label", label);
    })
    .catch(function () {
      healthDot.className = "health-dot down";
      healthDot.title = "Backend unavailable";
      healthDot.setAttribute("aria-label", "Backend unavailable");
    });
}

// ── Voice session management ─────────────────────

var voiceFab = document.getElementById("voice-fab");
var voiceWidget = document.getElementById("voice-widget");
var voiceEndBtn = document.getElementById("voice-end-btn");
var voiceStateDot = document.getElementById("voice-state-dot");
var voiceStateText = document.getElementById("voice-state-text");
var voiceVisualizer = document.getElementById("voice-visualizer");
var voiceTranscript = document.getElementById("voice-transcript");
var voiceBars = voiceVisualizer ? voiceVisualizer.querySelectorAll(".voice-bar") : [];

var voiceRoom = null;
var voiceAudioElement = null;
var voiceAnalyserInterval = null;
var greetingDone = false;
var lastAgentState = "";
var voicePreviouslyFocused = null;

// ── Voice dialog focus management (role="dialog" aria-modal) ──

function getVoiceFocusable() {
  if (!voiceWidget) return [];
  return voiceWidget.querySelectorAll(
    'button, [href], input, textarea, [tabindex]:not([tabindex="-1"])'
  );
}

function handleVoiceKeydown(e) {
  if (e.key === "Escape") {
    e.preventDefault();
    endVoiceSession();
    return;
  }
  if (e.key !== "Tab") return;
  var focusable = getVoiceFocusable();
  if (focusable.length === 0) return;
  var first = focusable[0];
  var last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function setVoiceState(state, label) {
  voiceStateDot.className = "voice-state-dot " + state;
  voiceStateText.textContent = label || state;
}

function addTranscriptEntry(role, text) {
  var entry = document.createElement("div");
  entry.className = "voice-transcript-entry " + role;
  entry.textContent = text;
  voiceTranscript.appendChild(entry);
  voiceTranscript.scrollTop = voiceTranscript.scrollHeight;
}

function startVisualizer(stream) {
  if (!stream || !window.AudioContext) return;
  try {
    var audioCtx = new AudioContext();
    var source = audioCtx.createMediaStreamSource(stream);
    var analyser = audioCtx.createAnalyser();
    analyser.fftSize = 64;
    source.connect(analyser);
    var data = new Uint8Array(analyser.frequencyBinCount);
    voiceAnalyserInterval = setInterval(function () {
      analyser.getByteFrequencyData(data);
      for (var i = 0; i < voiceBars.length; i++) {
        var val = data[i * 2] || 0;
        var h = Math.max(4, (val / 255) * 48);
        voiceBars[i].style.height = h + "px";
      }
    }, 80);
  } catch (_e) { /* audio context not available */ }
}

function stopVisualizer() {
  if (voiceAnalyserInterval) {
    clearInterval(voiceAnalyserInterval);
    voiceAnalyserInterval = null;
  }
  for (var i = 0; i < voiceBars.length; i++) {
    voiceBars[i].style.height = "4px";
  }
}

function clearVoiceTranscript() {
  while (voiceTranscript.firstChild) {
    voiceTranscript.removeChild(voiceTranscript.firstChild);
  }
}

async function startVoiceSession() {
  var LK = window.LivekitClient;
  if (!LK) {
    console.error("LivekitClient not found on window. Available globals:", Object.keys(window).filter(function(k) { return k.toLowerCase().indexOf("livekit") >= 0; }));
    announce("Voice client library not loaded. Please refresh the page.");
    return;
  }
  console.log("Starting voice session, LivekitClient loaded:", !!LK);

  voicePreviouslyFocused = document.activeElement;
  voiceWidget.hidden = false;
  voiceFab.hidden = true;
  voiceWidget.addEventListener("keydown", handleVoiceKeydown);
  // Move focus into the dialog so keyboard/SR users land inside it.
  if (voiceEndBtn) voiceEndBtn.focus();
  clearVoiceTranscript();
  greetingDone = false;
  lastAgentState = "";
  setVoiceState("", "Connecting...");

  try {
    // Fetch token from backend
    var resp = await fetch("/api/voice/token", { method: "POST" });
    if (!resp.ok) {
      var errData = await resp.json().catch(function () { return {}; });
      if (resp.status === 401 || resp.status === 503) {
        throw new Error("Voice chat is unavailable right now. Please use chat or call the campus phone line.");
      }
      throw new Error(errData.detail || "Voice agent not available");
    }
    var tokenData = await resp.json();

    // Connect to LiveKit room
    voiceRoom = new LK.Room({
      adaptiveStream: true,
      dynacast: true,
    });

    // Handle remote tracks (agent audio)
    voiceRoom.on(LK.RoomEvent.TrackSubscribed, function (track, publication, participant) {
      if (track.kind === LK.Track.Kind.Audio) {
        voiceAudioElement = track.attach();
        voiceAudioElement.style.display = "none";
        document.body.appendChild(voiceAudioElement);
      }
    });

    voiceRoom.on(LK.RoomEvent.TrackUnsubscribed, function (track) {
      if (voiceAudioElement) {
        track.detach(voiceAudioElement);
        if (voiceAudioElement.parentElement) {
          voiceAudioElement.parentElement.removeChild(voiceAudioElement);
        }
        voiceAudioElement = null;
      }
    });

    // Handle transcription events
    voiceRoom.on(LK.RoomEvent.TranscriptionReceived, function (segments, participant) {
      for (var i = 0; i < segments.length; i++) {
        var seg = segments[i];
        if (seg.final) {
          var isAgent = participant && participant.identity !== voiceRoom.localParticipant.identity;
          addTranscriptEntry(isAgent ? "agent" : "user", seg.text);
        }
      }
    });

    // Handle agent state via participant attributes (gated by greeting completion)
    voiceRoom.on(LK.RoomEvent.ParticipantAttributesChanged, function (changed, participant) {
      if (participant.identity === voiceRoom.localParticipant.identity) return;
      var agentState = participant.attributes && participant.attributes["lk.agent.state"];
      if (!agentState) return;

      // Detect greeting completion: agent transitions from speaking → listening
      if (lastAgentState === "speaking" && agentState === "listening") {
        greetingDone = true;
      }
      lastAgentState = agentState;

      var labels = {
        listening: "Listening...",
        thinking: "Thinking...",
        speaking: "Speaking...",
        initializing: "Setting up...",
      };

      if (greetingDone) {
        // After greeting: honor all state changes normally
        setVoiceState(agentState, labels[agentState] || agentState);
      } else {
        // Before greeting done: only show "Speaking..." if agent is speaking,
        // otherwise stay on "Starting up..."
        if (agentState === "speaking") {
          setVoiceState("speaking", "Speaking...");
        }
        // For other states (listening, thinking), keep showing "Starting up..."
      }
    });

    voiceRoom.on(LK.RoomEvent.Disconnected, function () {
      endVoiceSession();
    });

    await voiceRoom.connect(tokenData.url, tokenData.token);
    setVoiceState("thinking", "Starting up...");
    setTimeout(function () { greetingDone = true; }, 8000);

    // Enable microphone
    await voiceRoom.localParticipant.setMicrophoneEnabled(true);

    // Start audio visualizer from local mic stream
    var micPub = voiceRoom.localParticipant.getTrackPublication(LK.Track.Source.Microphone);
    if (micPub && micPub.track) {
      var mediaStream = micPub.track.mediaStream;
      if (mediaStream) startVisualizer(mediaStream);
    }
  } catch (err) {
    console.error("Voice session error:", err);
    var msg = err.message || "Unknown error";
    if (msg.toLowerCase().indexOf("permission") >= 0 || msg.toLowerCase().indexOf("not allowed") >= 0) {
      msg = "Microphone access denied. Please allow microphone permission and try again.";
    }
    setVoiceState("", msg);
    setTimeout(endVoiceSession, 5000);
  }
}

function endVoiceSession() {
  stopVisualizer();
  if (voiceRoom) {
    voiceRoom.disconnect();
    voiceRoom = null;
  }
  if (voiceAudioElement && voiceAudioElement.parentElement) {
    voiceAudioElement.parentElement.removeChild(voiceAudioElement);
    voiceAudioElement = null;
  }
  voiceWidget.removeEventListener("keydown", handleVoiceKeydown);
  voiceWidget.hidden = true;
  voiceFab.hidden = false;
  // Return focus to the trigger (or wherever it was) per WCAG 2.4.3.
  var restoreTo = voicePreviouslyFocused && document.contains(voicePreviouslyFocused)
    ? voicePreviouslyFocused
    : voiceFab;
  if (restoreTo && restoreTo.focus) restoreTo.focus();
  voicePreviouslyFocused = null;
}

if (voiceFab) {
  voiceFab.addEventListener("click", startVoiceSession);
}
if (voiceEndBtn) {
  voiceEndBtn.addEventListener("click", endVoiceSession);
}

// ── Init ──────────────────────────────────────────

showWelcome();
fetchHealth();
