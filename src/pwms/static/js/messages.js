/**
 * Fade the site's flash messages out.
 *
 * The boxes are rendered server-side at the top of every page (see
 * pwms/partials/messages.html). They are notices about what the last action did —
 * the page underneath already shows the result — so each one fades out on its
 * own, and a reader can dismiss one early.
 *
 * Sliding or hovering over a message holds it open, so a reader part-way through
 * reading one is not racing the clock, and the countdown restarts when they leave
 * (tabbing into the close button counts as hovering, via focusin/focusout).
 */
(function () {
    "use strict";

    // Keep in step with the .site-message transition in static/css/style.css.
    var VISIBLE_MS = 6000;
    var FADE_MS = 400;

    var container = document.getElementById("site-messages");
    if (!container) {
        return;
    }

    function dismiss(message) {
        if (message.classList.contains("site-message--closing")) {
            return;
        }
        message.classList.add("site-message--closing");
        window.setTimeout(function () {
            message.remove();
            // The stack is only as tall as its boxes, so take it with the last
            // one rather than leave an empty sticky band behind.
            if (!container.querySelector(".site-message")) {
                container.remove();
            }
        }, FADE_MS);
    }

    Array.prototype.forEach.call(
        container.querySelectorAll(".site-message"),
        function (message) {
            var timer = null;
            var dismissed = false;

            function start() {
                if (dismissed || timer !== null) {
                    return;
                }
                timer = window.setTimeout(function () {
                    timer = null;
                    dismiss(message);
                }, VISIBLE_MS);
            }

            function hold() {
                if (timer !== null) {
                    window.clearTimeout(timer);
                    timer = null;
                }
            }

            start();

            message.addEventListener("mouseenter", hold);
            message.addEventListener("mouseleave", start);
            message.addEventListener("focusin", hold);
            message.addEventListener("focusout", start);

            var close = message.querySelector("[data-message-dismiss]");
            if (close) {
                close.addEventListener("click", function () {
                    dismissed = true;
                    hold();
                    dismiss(message);
                });
            }
        }
    );
})();
