/*
 * Site theme toggle.
 *
 * Deliberately the same contract as Django's admin toggle (admin/js/theme.js) so the
 * admin and the public site behave identically: `data-theme` on <html> is "auto",
 * "light" or "dark", the choice is remembered in localStorage under "theme", and the
 * stylesheets consume it through `color-scheme` (see parl-light-dark.css).
 *
 * Loaded WITHOUT `defer` so `data-theme` is set during parsing, before the first
 * paint, which avoids a flash of the wrong theme.
 */
(function () {
    "use strict";

    var STORAGE_KEY = "theme";
    var MODES = ["auto", "light", "dark"];

    function readStored() {
        // localStorage throws on file:// pages and in some privacy modes.
        try {
            return localStorage.getItem(STORAGE_KEY);
        } catch (error) {
            return null;
        }
    }

    function store(mode) {
        try {
            localStorage.setItem(STORAGE_KEY, mode);
        } catch (error) {
            // Not fatal: the theme still applies for this page view.
        }
    }

    function setTheme(mode) {
        if (MODES.indexOf(mode) === -1) {
            console.error("Got invalid theme mode: " + mode + ". Resetting to auto.");
            mode = "auto";
        }
        document.documentElement.dataset.theme = mode;
        store(mode);
    }

    function cycleTheme() {
        var current = readStored() || "auto";
        // Same order as the admin: step away from whatever "auto" currently resolves to,
        // so the first click always produces a visible change.
        var prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        var order = prefersDark ? ["auto", "light", "dark"] : ["auto", "dark", "light"];
        setTheme(order[(order.indexOf(current) + 1) % order.length]);
    }

    function bindToggles() {
        Array.prototype.forEach.call(
            document.querySelectorAll(".theme-toggle"),
            function (button) {
                button.addEventListener("click", cycleTheme);
            },
        );
    }

    setTheme(readStored() || "auto");
    // Progressive enhancement: without JS the button could not toggle anything, so the
    // CSS only reveals it once this class is present.
    document.documentElement.classList.add("theme-ready");

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bindToggles);
    } else {
        bindToggles();
    }
})();
