"use strict";

/**
 * The header's global search panel.
 *
 * The input (see templates/header.html) asks HTMX for the categorized results
 * fragment and swaps it into #global-search-results. This script owns only what
 * HTMX cannot: whether the panel is shown, and closing it.
 *
 * The fragment renders a .global-search-panel when there is anything to show and
 * nothing at all when the box is empty, so "does it contain a panel?" is exactly
 * the shown / hidden test — there is no separate empty state to keep in sync.
 */
(function () {
    const ROOT = "[data-global-search]";
    const RESULTS = "#global-search-results";
    const PANEL = ".global-search-panel";

    /** @returns {Element|null} */
    function root() {
        return document.querySelector(ROOT);
    }

    /** @returns {Element|null} */
    function results() {
        return document.querySelector(RESULTS);
    }

    /** Show the panel only when the fragment actually put something in it. */
    function sync() {
        const el = results();
        if (!el) {
            return;
        }
        const hasPanel = Boolean(el.querySelector(PANEL));
        el.classList.toggle("d-none", !hasPanel);
        const input = root() ? root().querySelector("input") : null;
        if (input) {
            input.setAttribute("aria-expanded", String(hasPanel));
        }
    }

    function close() {
        const el = results();
        if (el) {
            el.classList.add("d-none");
        }
    }

    // HTMX reports the swap on the element whose innerHTML changed.
    document.body.addEventListener("htmx:afterSwap", function (event) {
        const el = results();
        if (el && (event.target === el || el.contains(event.target))) {
            sync();
        }
    });

    // Escape closes the panel and returns focus to the box being typed in.
    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") {
            return;
        }
        const el = root();
        if (el && el.contains(document.activeElement)) {
            close();
            const input = el.querySelector("input");
            if (input) {
                input.focus();
            }
        }
    });

    // A click anywhere outside the box dismisses the panel.
    document.addEventListener("click", function (event) {
        const el = root();
        if (el && !el.contains(event.target)) {
            close();
        }
    });
})();
