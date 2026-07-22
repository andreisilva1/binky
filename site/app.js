/* Binky — landing behaviour. No dependencies, no build step. */
(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------------------------------------------------------------- nav ---- */
  var nav = document.getElementById("nav");
  if (nav) {
    var onScroll = function () {
      nav.classList.toggle("scrolled", window.scrollY > 8);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  /* ------------------------------------------------------- copy-to-clipboard */
  document.querySelectorAll(".cmd-chip[data-copy]").forEach(function (chip) {
    var label = chip.querySelector(".copy");
    var original = label ? label.textContent : "";
    var timer;

    chip.addEventListener("click", function () {
      var text = chip.getAttribute("data-copy");
      var done = function () {
        if (!label) return;
        chip.classList.add("copied");
        label.textContent = "copied";
        clearTimeout(timer);
        timer = setTimeout(function () {
          chip.classList.remove("copied");
          label.textContent = original;
        }, 1600);
      };

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, fallback);
      } else {
        fallback();
      }

      function fallback() {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try {
          document.execCommand("copy");
          done();
        } catch (e) {
          /* clipboard unavailable — leave the label alone */
        }
        document.body.removeChild(ta);
      }
    });
  });

  /* --------------------------------------------------------- scroll reveal -- */
  var revealables = document.querySelectorAll(".reveal");
  if (reduced || !("IntersectionObserver" in window)) {
    revealables.forEach(function (el) {
      el.classList.add("in");
    });
  } else {
    var revealObserver = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry, i) {
          if (!entry.isIntersecting) return;
          var el = entry.target;
          setTimeout(function () {
            el.classList.add("in");
          }, i * 70);
          revealObserver.unobserve(el);
        });
      },
      { rootMargin: "0px 0px -12% 0px", threshold: 0.12 }
    );
    revealables.forEach(function (el) {
      revealObserver.observe(el);
    });
  }

  /* ------------------------------------------------------ docs sidebar spy -- */
  /* Highlights the section you're reading. Driven by scroll position rather than
     IntersectionObserver: with headings of very different lengths, "which one is
     visible" is ambiguous, while "the last one you scrolled past" never is. */
  var toc = document.getElementById("toc");
  if (toc) {
    var links = Array.prototype.slice.call(toc.querySelectorAll('a[href^="#"]'));
    var targets = links
      .map(function (a) {
        return { link: a, el: document.getElementById(a.getAttribute("href").slice(1)) };
      })
      .filter(function (t) {
        return t.el;
      });

    if (targets.length) {
      var ticking = false;

      var sync = function () {
        ticking = false;
        var line = window.scrollY + 120; /* just below the sticky nav */
        var current = targets[0];

        for (var i = 0; i < targets.length; i++) {
          if (targets[i].el.getBoundingClientRect().top + window.scrollY <= line) {
            current = targets[i];
          }
        }
        /* the last section is usually too short to ever reach the line */
        if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 4) {
          current = targets[targets.length - 1];
        }

        targets.forEach(function (t) {
          t.link.classList.toggle("on", t === current);
        });
      };

      window.addEventListener(
        "scroll",
        function () {
          if (ticking) return;
          ticking = true;
          window.requestAnimationFrame(sync);
        },
        { passive: true }
      );
      sync();
    }
  }
})();
