/* Trade Scale - tiny progressive-enhancement script (no framework, no CDN).
   Keeps the app dependency-free so a strict Content-Security-Policy can forbid
   all third-party / inline scripts. */
(function () {
  "use strict";

  // --- live total of the factor-weight inputs (Settings) --------------------
  function sumWeights() {
    var total = 0;
    document.querySelectorAll(".w-input").forEach(function (i) {
      total += parseFloat(i.value) || 0;
    });
    var el = document.getElementById("wsum");
    if (el) el.textContent = total.toFixed(2);
  }
  document.querySelectorAll(".w-input").forEach(function (i) {
    i.addEventListener("input", sumWeights);
  });
  sumWeights();

  // --- "Test key" button (replaces the old htmx call) -----------------------
  var testBtn = document.getElementById("test-key-btn");
  if (testBtn) {
    testBtn.addEventListener("click", function () {
      var input = document.querySelector('input[name="openai_api_key"]');
      var target = document.getElementById("key-test-result");
      if (!target) return;
      target.innerHTML = '<p class="alert small">Testing the key…</p>';
      var body = new URLSearchParams();
      body.append("openai_api_key", input ? input.value : "");
      fetch("/settings/test-key", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString()
      })
        .then(function (r) { return r.text(); })
        .then(function (htmlFragment) { target.innerHTML = htmlFragment; })
        .catch(function () {
          target.innerHTML = '<p class="alert alert-veto small">Test failed to run.</p>';
        });
    });
  }

  // --- confirm() for destructive forms, without inline handlers -------------
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // --- live session tracker: refresh while the market is open ---------------
  // The frozen morning verdict is never touched; only #live-panel updates. The
  // server marks the fragment with data-poll="1" only during the live session,
  // so polling starts and stops on its own.
  var livePanel = document.getElementById("live-panel");
  if (livePanel) {
    var POLL_MS = 120000; // 2 min - intraday efficiency moves slowly; be gentle to yfinance
    var shouldPoll = function () {
      var m = livePanel.querySelector("[data-poll]");
      return !!m && m.getAttribute("data-poll") === "1";
    };
    var schedule = function () { if (shouldPoll()) window.setTimeout(refresh, POLL_MS); };
    var refresh = function () {
      fetch("/live-panel")
        .then(function (r) { return r.text(); })
        .then(function (htmlFragment) { livePanel.innerHTML = htmlFragment; schedule(); })
        .catch(function () { schedule(); });
    };
    schedule(); // only fires if the initial server render says we're live
  }
})();
