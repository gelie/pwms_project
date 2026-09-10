/*
 * ARIA for the hover-opened desktop dropdowns.
 *
 * The main nav opens its dropdowns on hover in the expanded bar (see the
 * `(min-width: 992px) and (hover: hover) and (pointer: fine)` rule in style.css).
 * Pure CSS can't update ARIA, so screen readers kept seeing aria-expanded="false"
 * while the menu was visibly open. This mirrors the hover / focus-within state onto
 * the toggle.
 *
 * It deliberately does NOT call Bootstrap's dropdown API: the visual behaviour and
 * Bootstrap's own click/keyboard state stay exactly as they were. Inside the
 * offcanvas (below 992px) Bootstrap's tap/click toggle is the only thing that opens
 * menus and already manages aria-expanded, so the handlers are removed there.
 */
(function () {
    "use strict";

    // Keep in sync with the hover media query in style.css.
    var DESKTOP = "(min-width: 992px) and (hover: hover) and (pointer: fine)";
    var SELECTOR = "#main-horiz-menu .nav-item.dropdown";
    var BAR_SELECTOR = "#main-horiz-menu";
    var EVENTS = [
        "mouseenter",
        "mouseleave",
        "focusin",
        "focusout",
        "shown.bs.dropdown",
        "hidden.bs.dropdown",
    ];
    // Leaving the bar without touching an item still changes what the CSS renders
    // (a focused menu comes back), so the bar itself needs listeners too.
    var BAR_EVENTS = ["mouseenter", "mouseleave"];

    /**
     * Work out what the CSS is actually showing and report it truthfully.
     *
     * The event name matters because :hover / :focus-within may not have been
     * recomputed yet when the event fires. On mouseleave we know the pointer is
     * gone, and on focusout document.activeElement already points at the new
     * target, so neither has to be guessed.
     */
    function syncItem(item, phase) {
        var menu = item.querySelector(".dropdown-menu");
        var toggle = item.querySelector(".dropdown-toggle");
        if (!menu || !toggle) {
            return;
        }
        var hovering = phase === "mouseleave" ? false : item.matches(":hover");
        var focused =
            phase === "focusout" ? item.contains(document.activeElement) : item.matches(":focus-within");

        // Mirrors the one-menu-at-a-time rule in style.css: while the pointer is inside
        // the bar only the hovered item's menu is rendered, so a focused (or
        // click-opened) sibling must not claim to be expanded.
        var bar = item.closest(BAR_SELECTOR);
        var barHovered = bar ? bar.matches(":hover") : false;
        var elsewhere = focused || menu.classList.contains("show");

        var open = hovering || (!barHovered && elsewhere);
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }

    function syncAll() {
        Array.prototype.forEach.call(document.querySelectorAll(SELECTOR), function (item) {
            syncItem(item, "sync");
        });
    }

    function handle(event) {
        // Hovering one item changes what the CSS renders for its siblings too, so
        // settle everything and then apply this event's own phase override.
        syncAll();
        syncItem(event.currentTarget, event.type);
    }

    function handleBar() {
        syncAll();
    }

    function bind() {
        Array.prototype.forEach.call(document.querySelectorAll(SELECTOR), function (item) {
            if (item.dataset.hoverAriaBound === "true") {
                return;
            }
            EVENTS.forEach(function (name) {
                item.addEventListener(name, handle);
            });
            item.dataset.hoverAriaBound = "true";
        });
        Array.prototype.forEach.call(document.querySelectorAll(BAR_SELECTOR), function (bar) {
            if (bar.dataset.hoverAriaBound === "true") {
                return;
            }
            BAR_EVENTS.forEach(function (name) {
                bar.addEventListener(name, handleBar);
            });
            bar.dataset.hoverAriaBound = "true";
        });
        // Re-sync straight away: a resize can put an item under a stationary pointer
        // without firing any mouse event, which would otherwise leave a menu rendered
        // by :hover reporting aria-expanded="false".
        syncAll();
    }

    function unbind() {
        Array.prototype.forEach.call(document.querySelectorAll(SELECTOR), function (item) {
            if (item.dataset.hoverAriaBound !== "true") {
                return;
            }
            EVENTS.forEach(function (name) {
                item.removeEventListener(name, handle);
            });
            delete item.dataset.hoverAriaBound;
            // Hand the attribute back to Bootstrap in whatever state it left it.
            var menu = item.querySelector(".dropdown-menu");
            var toggle = item.querySelector(".dropdown-toggle");
            if (menu && toggle) {
                toggle.setAttribute("aria-expanded", menu.classList.contains("show") ? "true" : "false");
            }
        });
        Array.prototype.forEach.call(document.querySelectorAll(BAR_SELECTOR), function (bar) {
            if (bar.dataset.hoverAriaBound !== "true") {
                return;
            }
            BAR_EVENTS.forEach(function (name) {
                bar.removeEventListener(name, handleBar);
            });
            delete bar.dataset.hoverAriaBound;
        });
    }

    var desktop = window.matchMedia(DESKTOP);

    function apply() {
        if (desktop.matches) {
            bind();
        } else {
            unbind();
        }
    }

    apply();
    if (desktop.addEventListener) {
        desktop.addEventListener("change", apply);
    } else if (desktop.addListener) {
        desktop.addListener(apply);
    }
})();
