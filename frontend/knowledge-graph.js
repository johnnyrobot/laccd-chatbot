/* Guided tour of one college's knowledge graph.

   A single SVG holds three scenes at different scales: the district, one
   college inside the district, and one course inside the college. Moving
   between them is a true zoom, so the audience sees where each picture sits
   in the one before it. The captions are already in the page, which is what
   no-JS and phone visitors read. */

(function () {
  "use strict";

  var W = 1600;
  var H = 680;
  var CX = W / 2;
  var CY = H / 2;
  var COURSES_PER_DOT = 50;
  var DOT_SPACING = 8;
  var ZOOM_MS = 1150;
  var SVG_NS = "http://www.w3.org/2000/svg";

  var data = JSON.parse(document.getElementById("kg-data").textContent);
  var mount = document.getElementById("kg-diagram");
  var stage = document.getElementById("kg-stage");
  var stepEls = Array.prototype.slice.call(document.querySelectorAll(".kg-step"));
  var keys = stepEls.map(function (el) { return el.dataset.key; });
  var narrow = window.matchMedia("(max-width: 820px)");
  var calm = window.matchMedia("(prefers-reduced-motion: reduce)");

  var DEPTH = { district: 0, campus: 1 };
  function depthOf(key) {
    return key in DEPTH ? DEPTH[key] : 2;
  }

  /* Each tour step lights the parts of the course scene it talks about. */
  var LIT = {
    course: ["hero"],
    renumber: ["hero", "old"],
    path: ["hero", "chain", "unlocks"],
    counts: ["hero", "transfer", "ge", "programs", "careers"],
    answer: ["hero", "near", "transfer", "ge", "old"],
  };

  function el(name, attrs, parent, text) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs || {}).forEach(function (key) {
      node.setAttribute(key, attrs[key]);
    });
    if (text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }

  function fmt(n) {
    return n === undefined || n === null ? "" : Number(n).toLocaleString("en-US");
  }

  function clip(text, max) {
    text = String(text || "");
    if (text.length <= max) return text;
    var cut = text.slice(0, max - 1);
    var space = cut.lastIndexOf(" ");
    return (space > max * 0.5 ? cut.slice(0, space) : cut).trimEnd() + "…";
  }

  /* Up to two lines, broken at a space. */
  function wrap(text, max) {
    text = String(text || "");
    if (text.length <= max) return [text];
    var cut = text.lastIndexOf(" ", max);
    if (cut < 8) cut = max;
    return [text.slice(0, cut).trim(), clip(text.slice(cut).trim(), max)];
  }

  /* A labeled rounded box centered on (x, y). */
  function pill(parent, x, y, w, h, lines, opts) {
    opts = opts || {};
    var g = el("g", {
      class: "kg-pill" + (opts.solid ? " is-solid" : "") + (opts.old ? " is-old" : "") + (opts.hop ? " kg-hop" : ""),
    }, parent);
    if (opts.hop) g.dataset.hop = opts.hop;
    el("rect", { x: x - w / 2, y: y - h / 2, width: w, height: h, rx: opts.rx || 14 }, g);
    var total = lines.reduce(function (sum, line) { return sum + line.size * 1.2; }, 0);
    var cursor = y - total / 2;
    lines.forEach(function (line) {
      cursor += line.size * 1.2;
      el("text", {
        x: x,
        y: cursor - line.size * 0.26,
        "text-anchor": "middle",
        "font-size": line.size,
        "font-weight": line.weight || 500,
      }, g, line.text);
    });
    return g;
  }

  function line(parent, x1, y1, x2, y2, hop, curve) {
    var d = curve
      ? "M" + x1 + " " + y1 + " C" + (x1 + curve) + " " + y1 + " " + (x2 - curve) + " " + y2 + " " + x2 + " " + y2
      : "M" + x1 + " " + y1 + " L" + x2 + " " + y2;
    var path = el("path", { d: d, class: "kg-line" + (hop ? " kg-hop" : ""), pathLength: 1 }, parent);
    if (hop) path.dataset.hop = hop;
    return path;
  }

  function arrow(path) {
    path.setAttribute("marker-end", "url(#kg-arrow)");
    return path;
  }

  function label(parent, x, y, text, hop, anchor) {
    var node = el("text", {
      x: x,
      y: y,
      "text-anchor": anchor || "middle",
      "font-size": 21,
      class: hop ? "kg-hop" : "",
    }, parent, text);
    if (hop) node.dataset.hop = hop;
    return node;
  }

  /* ── District: nine colleges, drawn to scale in dots ─────────────── */

  /* Hand-placed so nine discs of different sizes never crowd each other. */
  var SLOTS = [
    [800, 86], [1240, 100], [1440, 306], [1270, 520], [960, 548],
    [640, 548], [330, 520], [160, 306], [360, 100],
  ];
  var HUB_Y = 318;

  function collegeLayout() {
    return data.colleges.map(function (code, i) {
      var courses = data.courses ? data.courses[code].Course : 1500;
      var dots = Math.max(1, Math.round(courses / COURSES_PER_DOT));
      return {
        code: code,
        x: SLOTS[i][0],
        y: SLOTS[i][1],
        dots: dots,
        r: Math.sqrt(dots) * DOT_SPACING,
      };
    });
  }

  function drawDistrict(parent, colleges) {
    colleges.forEach(function (c) {
      var endY = c.x === CX ? c.y + c.r + 48 : c.y;
      line(parent, CX, HUB_Y, c.x, endY).setAttribute("opacity", c.code === data.campus ? 0.9 : 0.28);
    });

    pill(parent, CX, HUB_Y, 330, 112, [
      { text: "Shared by all nine", size: 27, weight: 600 },
      { text: "GE areas · Careers · Textbooks", size: 21, weight: 400 },
    ]);

    colleges.forEach(function (c) {
      var mine = c.code === data.campus;
      var g = el("g", { class: "kg-college" }, parent);
      var link = el("a", { href: "https://" + c.code.toLowerCase() + ".chat/knowledge-graph", tabindex: "-1" }, g);
      el("circle", { cx: c.x, cy: c.y, r: c.r + 8, fill: "var(--stage)" }, link);
      /* Sunflower spiral: an even disc whose area tracks the course count. */
      for (var i = 0; i < c.dots; i++) {
        var radius = DOT_SPACING * Math.sqrt(i + 0.5);
        var theta = i * 2.39996;
        el("circle", {
          cx: c.x + Math.cos(theta) * radius,
          cy: c.y + Math.sin(theta) * radius,
          r: 3.1,
          fill: "#ffffff",
          opacity: mine ? 1 : 0.55,
        }, link);
      }
      if (mine) {
        el("circle", { cx: c.x, cy: c.y, r: c.r + 10, fill: "none", stroke: "#ffffff", "stroke-width": 3 }, link);
      }
      el("text", {
        x: c.x,
        y: c.y + c.r + 36,
        "text-anchor": "middle",
        "font-size": 25,
        "font-weight": mine ? 700 : 500,
      }, link, c.code);
    });
  }

  /* ── College: every kind of thing in the map ─────────────────────── */

  function drawCampus(parent) {
    var c = data.counts || {};
    var e = c.edges || {};
    var count = function (n) { return n === undefined ? [] : [{ text: fmt(n), size: 40, weight: 600 }]; };
    var edgeLabel = function (text, n) { return n === undefined ? text : text + " · " + fmt(n); };

    arrow(line(parent, 955, 340, 1085, 340));
    arrow(line(parent, 1295, 340, 1410, 340));
    arrow(line(parent, 800, 272, 800, 150));
    arrow(line(parent, 480, 205, 645, 300));
    arrow(line(parent, 480, 475, 645, 380));
    arrow(line(parent, 180, 170, 270, 170));
    arrow(el("path", { d: "M735 408 C735 492 865 492 865 410", class: "kg-line" }, parent));

    pill(parent, 800, 340, 310, 136, count(c.Course).concat([{ text: "Course records", size: 27 }]), { solid: true });
    pill(parent, 1190, 340, 210, 112, count(c.Program).concat([{ text: "Programs", size: 24 }]));
    pill(parent, 1500, 340, 180, 112, count(c.careers).concat([{ text: "Careers", size: 24 }]));
    pill(parent, 800, 92, 210, 112, count(c.ge_areas).concat([{ text: "GE areas", size: 24 }]));
    pill(parent, 375, 170, 210, 112, count(c.Subject).concat([{ text: "Subjects", size: 24 }]));
    pill(parent, 375, 510, 210, 112, count(c.Instructor).concat([{ text: "Instructors", size: 24 }]));
    pill(parent, 100, 170, 160, 112, count(c.books).concat([{ text: "Textbooks", size: 24 }]));

    label(parent, 1020, 324, "are in");
    label(parent, 1020, 372, fmt(e.REQUIRES_COURSE));
    label(parent, 1352, 324, "lead to");
    label(parent, 1352, 372, fmt(e.LEADS_TO_CAREER));
    label(parent, 822, 214, edgeLabel("count toward", e.FULFILLS_GE), null, "start");
    label(parent, 600, 236, "contain");
    label(parent, 545, 412, edgeLabel("teach", e.TEACHES), null, "end");
    label(parent, 225, 148, "used in");

    var linked = (e.PREREQUISITE_OF || 0) + (e.COREQUISITE_OF || 0) + (e.ADVISORY_FOR || 0) + (e.RENUMBERED_TO || 0);
    label(parent, 800, 526, (linked ? fmt(linked) + " connections between courses" : "Connections between courses"));
    label(parent, 800, 556, "prerequisites, corequisites, advisories, renumberings");
  }

  /* ── Course: one real neighborhood ───────────────────────────────── */

  function courseLines(course) {
    return [{ text: clip(course.code, 12), size: 24, weight: 600 }].concat(
      wrap(course.title, 19).map(function (text) {
        return { text: text, size: 17, weight: 400 };
      })
    );
  }

  function drawCourse(parent, w) {
    var heroY = 330;

    /* Old code, upper left, entering the top of the course. */
    if (w.old) {
      arrow(el("path", {
        d: "M325 110 C430 110 520 220 650 " + (heroY - 64),
        class: "kg-line kg-hop",
        pathLength: 1,
        "data-hop": "old",
      }, parent));
      pill(parent, 220, 110, 210, 92, [
        { text: w.old, size: 28, weight: 600 },
        { text: "old number", size: 20, weight: 400 },
      ], { hop: "old", old: true });
      label(parent, 345, 92, "renumbered as", "old", "start");
    }

    /* Prerequisite chain, flowing left to right into the course. */
    var chain = w.chain || [];
    var slot = function (i) { return 490 - (chain.length - 1 - i) * 196; };
    chain.forEach(function (course, i) {
      var x = slot(i);
      var nearest = i === chain.length - 1;
      var hop = "chain" + (nearest ? " near" : "");
      arrow(line(parent, x + 86, heroY, nearest ? 628 : slot(i + 1) - 87, heroY, hop));
      pill(parent, x, heroY, 172, 108, courseLines(course), { hop: hop });
    });
    if (chain.length) label(parent, 490, heroY + 84, "required before", "chain near");

    /* Courses this one opens, fanned above it. */
    var unlocks = (w.unlocks || []).slice();
    unlocks.forEach(function (course, i) {
      var x = 800 + (i - (unlocks.length - 1) / 2) * 188;
      arrow(line(parent, 800, heroY - 64, x, 166, "unlocks"));
      pill(parent, x, 110, 174, 108, courseLines(course), { hop: "unlocks" });
    });
    if (unlocks.length) {
      var opensX = 800 + ((unlocks.length - 1) / 2) * 188 + 104;
      var opens = w.unlocks_total > unlocks.length
        ? "opens up " + w.unlocks_total + " courses"
        : "opens up";
      label(parent, opensX, 118, opens, "unlocks", "start");
    }

    /* GE areas, below the course. */
    var ge = w.ge || [];
    ge.forEach(function (area, i) {
      var x = 800 + (i - (ge.length - 1) / 2) * 176;
      arrow(el("path", {
        d: "M800 " + (heroY + 62) + " V" + (heroY + 124) + " L" + x + " 530",
        class: "kg-line kg-hop",
        pathLength: 1,
        "data-hop": "ge",
      }, parent));
      pill(parent, x, 562, 162, 62, [{ text: area.system + " " + area.code, size: 21, weight: 600 }], { hop: "ge", rx: 12 });
    });
    if (ge.length) {
      label(parent, 800 - ((ge.length - 1) / 2) * 176 - 98, 570,
        w.ge_total > ge.length ? "counts toward " + w.ge_total + " GE areas" : "counts toward", "ge", "end");
    }

    /* Programs, and the careers those programs lead to. The bracket joins
       the two groups as a whole: the data behind this list is program-set
       to career-set, so no single program is paired with a single career. */
    var programs = w.programs || [];
    var rowY = function (i, n) { return heroY + (i - (n - 1) / 2) * 112; };
    programs.forEach(function (title, i) {
      var y = rowY(i, programs.length);
      arrow(line(parent, 970, heroY, 1048, y, "programs", 36));
      pill(parent, 1165, y, 230, 96, wrap(title, 20).map(function (text) {
        return { text: text, size: 20, weight: 500 };
      }), { hop: "programs" });
    });
    if (programs.length) {
      label(parent, 1165, rowY(programs.length - 1, programs.length) + 76,
        w.programs_total > programs.length ? "in " + w.programs_total + " programs" : "included in", "programs");
    }

    var careers = w.careers || [];
    if (careers.length && programs.length) {
      var spine = 1318;
      var rows = Math.max(programs.length, careers.length);
      var bracket = programs.map(function (_, i) {
        return "M1280 " + rowY(i, programs.length) + " H" + spine;
      });
      bracket.push("M" + spine + " " + rowY(0, rows) + " V" + rowY(rows - 1, rows));
      el("path", { d: bracket.join(" "), class: "kg-line kg-hop", pathLength: 1, "data-hop": "careers" }, parent);
      careers.forEach(function (_, i) {
        var y = rowY(i, careers.length);
        arrow(line(parent, spine, y, 1356, y, "careers"));
      });
    }
    careers.forEach(function (title, i) {
      pill(parent, 1468, rowY(i, careers.length), 220, 96, wrap(title, 21).map(function (text) {
        return { text: text, size: 19, weight: 500 };
      }), { hop: "careers" });
    });
    if (careers.length) {
      label(parent, 1468, rowY(careers.length - 1, careers.length) + 76,
        w.careers_total > careers.length ? "leads to " + w.careers_total + " careers" : "leads to", "careers");
    }

    /* Transfer flags hang directly off the course record. */
    var transfer = [w.uc ? "Transfers to UC" : "Not UC transferable", w.csu ? "Transfers to CSU" : "Not CSU transferable"];
    transfer.forEach(function (text, i) {
      pill(parent, 690 + i * 220, heroY + 94, 180, 46, [{ text: text, size: 18, weight: 600 }], { hop: "transfer", rx: 23 });
    });

    pill(parent, 800, heroY, 340, 124, [
      { text: w.code, size: 44, weight: 700 },
    ].concat(wrap(w.title, 30).map(function (text) {
      return { text: text, size: 21, weight: 500 };
    })), { hop: "hero", solid: true });
  }

  /* ── Scenes and camera ───────────────────────────────────────────── */

  function build() {
    var svg = el("svg", {
      class: "kg-svg",
      viewBox: "0 0 " + W + " " + H,
      role: "img",
      "aria-label": "Diagram of the knowledge graph. The steps below describe what it shows.",
    });
    var defs = el("defs", {}, svg);
    var marker = el("marker", {
      id: "kg-arrow",
      viewBox: "0 0 10 10",
      refX: 8,
      refY: 5,
      markerWidth: 5,
      markerHeight: 5,
      orient: "auto",
    }, defs);
    el("path", { d: "M0 0 L10 5 L0 10 z", fill: "#ffffff" }, marker);
    var camera = el("g", {}, svg);

    var colleges = collegeLayout();
    var home = colleges.filter(function (c) { return c.code === data.campus; })[0] || colleges[0];

    /* Each scene sits inside the shape it expands from. */
    var campusScale = (home.r * 2.6) / H;
    var campusOrigin = { x: home.x - CX * campusScale, y: home.y - CY * campusScale };
    var courseScale = campusScale * 0.2;
    var courseOrigin = {
      x: campusOrigin.x + campusScale * (CX - CX * 0.2),
      y: campusOrigin.y + campusScale * (CY - CY * 0.2),
    };

    function scene(origin, scale) {
      return el("g", {
        class: "kg-scene",
        transform: "translate(" + origin.x + " " + origin.y + ") scale(" + scale + ")",
      }, camera);
    }

    var scenes = [scene({ x: 0, y: 0 }, 1), scene(campusOrigin, campusScale), scene(courseOrigin, courseScale)];
    drawDistrict(scenes[0], colleges);
    drawCampus(scenes[1]);
    if (data.walk) drawCourse(scenes[2], data.walk);

    mount.appendChild(svg);

    function view(origin, scale) {
      var z = 1 / scale;
      return { z: z, x: -origin.x * z, y: -origin.y * z };
    }

    return {
      camera: camera,
      scenes: scenes,
      views: [view({ x: 0, y: 0 }, 1), view(campusOrigin, campusScale), view(courseOrigin, courseScale)],
    };
  }

  function applyCamera(world, v) {
    world.live = v;
    world.camera.setAttribute("transform", "translate(" + v.x + " " + v.y + ") scale(" + v.z + ")");
  }

  function setScene(world, depth, amount) {
    world.scenes.forEach(function (scene, i) {
      var shown = i === depth ? amount : 0;
      scene.setAttribute("opacity", shown);
      scene.setAttribute("visibility", shown > 0 ? "visible" : "hidden");
    });
  }

  /* Zooms logarithmically around the point the two views share, so the
     motion feels constant and the target stays put. */
  var running = 0;
  function fly(world, from, to, fromDepth, toDepth) {
    running += 1;
    var run = running;
    if (Math.abs(from.z - to.z) < 1e-6) {
      applyCamera(world, to);
      setScene(world, toDepth, 1);
      return;
    }
    var pivotX = (to.x - from.x) / (from.z - to.z);
    var pivotY = (to.y - from.y) / (from.z - to.z);
    var screenX = from.z * pivotX + from.x;
    var screenY = from.z * pivotY + from.y;
    var start = null;

    function frame(now) {
      if (run !== running) return;
      if (start === null) start = now;
      var t = Math.min(1, (now - start) / ZOOM_MS);
      var eased = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
      var z = Math.exp(Math.log(from.z) + (Math.log(to.z) - Math.log(from.z)) * eased);
      applyCamera(world, { z: z, x: screenX - z * pivotX, y: screenY - z * pivotY });

      /* The scenes overlap so the new one grows out of the old. The district
         can linger, but the college's large labels would collide with the
         course scene, so that zoom hands over sooner. */
      var linger = fromDepth === 0 || toDepth === 0;
      var out = 1 - Math.min(1, Math.max(0, (eased - (linger ? 0.5 : 0.15)) / (linger ? 0.4 : 0.35)));
      var inn = Math.min(1, Math.max(0, (eased - 0.3) / 0.45));
      world.scenes.forEach(function (scene, i) {
        var shown = i === fromDepth ? out : i === toDepth ? inn : 0;
        scene.setAttribute("opacity", shown);
        scene.setAttribute("visibility", shown > 0 ? "visible" : "hidden");
      });

      if (t < 1) {
        requestAnimationFrame(frame);
      } else {
        applyCamera(world, to);
        setScene(world, toDepth, 1);
      }
    }
    requestAnimationFrame(frame);
  }

  function highlight(world, key) {
    var scene = world.scenes[2];
    var lit = LIT[key] || [];
    scene.classList.toggle("is-overview", key === "course");
    scene.querySelectorAll("[data-hop]").forEach(function (part) {
      var on = part.dataset.hop.split(" ").some(function (name) { return lit.indexOf(name) !== -1; });
      part.classList.toggle("is-on", on);
    });
  }

  function start() {
    var world = build();
    var dots = document.getElementById("kg-dots");
    var prev = document.getElementById("kg-prev");
    var next = document.getElementById("kg-next");
    var full = document.getElementById("kg-full");
    var numbers = document.getElementById("kg-numbers");
    var index = 0;

    stepEls.forEach(function (stepEl, i) {
      var dot = document.createElement("button");
      dot.type = "button";
      dot.className = "kg-dot";
      dot.setAttribute("aria-label", "Go to step " + (i + 1) + " of " + stepEls.length);
      dot.addEventListener("click", function () { go(i); });
      dots.appendChild(dot);
    });

    function go(target, instant) {
      target = Math.max(0, Math.min(stepEls.length - 1, target));
      var fromDepth = depthOf(keys[index]);
      var toDepth = depthOf(keys[target]);
      index = target;

      stepEls.forEach(function (stepEl, i) {
        stepEl.classList.toggle("is-active", i === target);
      });
      Array.prototype.forEach.call(dots.children, function (dot, i) {
        if (i === target) dot.setAttribute("aria-current", "step");
        else dot.removeAttribute("aria-current");
      });
      prev.disabled = target === 0;
      next.textContent = target === stepEls.length - 1 ? "See the numbers" : "Next";
      highlight(world, keys[target]);
      history.replaceState(null, "", "#step-" + (target + 1));

      if (instant || calm.matches || fromDepth === toDepth) {
        running += 1;
        applyCamera(world, world.views[toDepth]);
        setScene(world, toDepth, 1);
      } else {
        fly(world, world.live, world.views[toDepth], fromDepth, toDepth);
      }
    }

    function forward() {
      if (index < stepEls.length - 1) go(index + 1);
      else numbers.scrollIntoView({ behavior: calm.matches ? "auto" : "smooth" });
    }

    function stageInView() {
      return stage.getBoundingClientRect().top > -window.innerHeight / 2;
    }

    prev.addEventListener("click", function () { go(index - 1); });
    next.addEventListener("click", forward);

    /* Presentation clickers send PageUp and PageDown. */
    document.addEventListener("keydown", function (event) {
      if (event.altKey || event.ctrlKey || event.metaKey || !stageInView() || narrow.matches) return;
      var onControl = /^(BUTTON|A)$/.test(document.activeElement.tagName);
      var key = event.key;
      if (key === "ArrowRight" || key === "PageDown" || (key === " " && !onControl)) {
        event.preventDefault();
        forward();
      } else if (key === "ArrowLeft" || key === "PageUp") {
        event.preventDefault();
        go(index - 1);
      } else if (key === "Home") {
        event.preventDefault();
        go(0);
      } else if (key === "End") {
        event.preventDefault();
        go(stepEls.length - 1);
      } else if (key === "f" || key === "F") {
        toggleFull();
      }
    });

    function toggleFull() {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen();
    }

    if (document.documentElement.requestFullscreen) {
      full.addEventListener("click", toggleFull);
      document.addEventListener("fullscreenchange", function () {
        var on = Boolean(document.fullscreenElement);
        full.setAttribute("aria-pressed", String(on));
        full.textContent = on ? "Exit full screen" : "Full screen";
      });
    } else {
      full.hidden = true;
    }

    document.getElementById("kg-controls").hidden = false;
    document.body.classList.add("kg-enhanced");

    var wanted = /^#step-(\d+)$/.exec(location.hash);
    go(wanted ? Number(wanted[1]) - 1 : 0, true);
  }

  (document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()).then(start);
})();
