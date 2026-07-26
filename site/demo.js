/* Binky — the walkthrough stepper. No dependencies, no build step.
   Pairs with the #demo section in index.html: the tokenised screens live in the
   markup, this only decides which one is on show, updates the caption, and types
   the leading command of each terminal screen. */
(function () {
  "use strict";

  var root = document.getElementById("demoStage");
  if (!root) return;

  var screens = Array.prototype.slice.call(root.querySelectorAll(".demo-screen"));
  if (!screens.length) return;

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var tabEl = document.getElementById("demoTab");
  var numEl = document.getElementById("demoNum");
  var titleEl = document.getElementById("demoTitle");
  var descEl = document.getElementById("demoDesc");
  var docEl = document.getElementById("demoDoc");
  var prevBtn = document.getElementById("demoPrev");
  var nextBtn = document.getElementById("demoNext");
  var dotsWrap = document.getElementById("demoDots");

  /* Caption text lives here; the terminal/editor content lives in the HTML. Both
     are kept truthful — the desc names the real verbs, the screens the real output. */
  var steps = [
    { title: "Install Binky",
      desc: "One command and <code>binky</code> is on your PATH — uv or pipx, your call.",
      doc: "install" },
    { title: "Describe your stack once",
      desc: "A single <code>binky.toml</code> at the repo root defines every service. <code>binky init</code> can scaffold it from an existing <code>docker-compose.yml</code>.",
      doc: "toml" },
    { title: "Check before anything runs",
      desc: "<code>binky check</code> validates the file and flags footguns — an inlined secret, a service that never binds a port — before a single process starts.",
      doc: "cli" },
    { title: "Bring a worktree up",
      desc: "<code>binky up login-fix</code> creates the worktree, reserves a free port block, clones the golden database, and supervises every service.",
      doc: "first" },
    { title: "Run branches in parallel",
      desc: "Another branch? <code>binky up checkout-v2</code>. Both stacks run at once, each on its own ports — nothing to stop first, no collisions.",
      doc: "ports" },
    { title: "Hand it to an agent",
      desc: "<code>binky env</code> prints one worktree's isolated coordinates. Feed them to an agent — or expose the same verbs over MCP with <code>claude mcp add binky -- binky mcp</code>.",
      doc: "agents" }
  ];

  var current = -1;
  var typeTimer = null;

  /* remember each command's full text so a screen is never left half-typed */
  screens.forEach(function (s) {
    var cmd = s.querySelector(".cmd");
    if (cmd) cmd.setAttribute("data-full", cmd.textContent);
  });

  var dots = steps.map(function (_, i) {
    var b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-label", "Step " + (i + 1));
    b.addEventListener("click", function () { go(i); });
    dotsWrap.appendChild(b);
    return b;
  });

  function typeCmd(cmd) {
    var full = cmd.getAttribute("data-full") || "";
    var screen = cmd.closest(".demo-screen");
    cmd.textContent = "";
    screen.classList.add("typing");
    var i = 0;
    (function tick() {
      cmd.textContent = full.slice(0, i);
      if (i >= full.length) {
        screen.classList.remove("typing");
        typeTimer = null;
        return;
      }
      var ch = full.charAt(i);
      i++;
      typeTimer = setTimeout(tick, 26 + Math.random() * 34 + (ch === " " ? 20 : 0));
    })();
  }

  function go(i, opts) {
    if (i < 0 || i >= steps.length || i === current) return;
    var animate = !(opts && opts.silent) && !reduced;
    current = i;

    /* cancel any in-flight typing and restore every command to its full text */
    if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; }
    screens.forEach(function (s) {
      s.classList.remove("typing", "is-active");
      var c = s.querySelector(".cmd");
      if (c) c.textContent = c.getAttribute("data-full") || "";
    });

    var screen = screens[i];
    screen.classList.add("is-active");

    if (tabEl) tabEl.textContent = screen.getAttribute("data-tab") || "";
    if (numEl) numEl.textContent = i + 1;
    if (titleEl) titleEl.textContent = steps[i].title;
    if (descEl) descEl.innerHTML = steps[i].desc;
    if (docEl) docEl.setAttribute("href", "./docs.html#" + steps[i].doc);

    dots.forEach(function (d, di) {
      d.classList.toggle("on", di === i);
      d.setAttribute("aria-selected", di === i ? "true" : "false");
    });
    if (prevBtn) prevBtn.disabled = i === 0;
    if (nextBtn) nextBtn.disabled = i === steps.length - 1;

    var cmd = screen.querySelector(".cmd");
    if (animate && cmd) typeCmd(cmd);
  }

  if (prevBtn) prevBtn.addEventListener("click", function () { go(current - 1); });
  if (nextBtn) nextBtn.addEventListener("click", function () { go(current + 1); });

  root.addEventListener("keydown", function (e) {
    if (e.key === "ArrowLeft") { go(current - 1); e.preventDefault(); }
    else if (e.key === "ArrowRight") { go(current + 1); e.preventDefault(); }
  });

  /* first step with no animation; type it once the demo scrolls into view */
  go(0, { silent: true });

  var firstCmd = screens[0].querySelector(".cmd");
  if (!reduced && firstCmd && "IntersectionObserver" in window) {
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          io.disconnect();
          if (current === 0 && !typeTimer) typeCmd(firstCmd);
        });
      },
      { threshold: 0.4 }
    );
    io.observe(root);
  } else if (reduced && firstCmd) {
    /* nothing to do — commands are already shown in full */
  }
})();
