/*
 * Show/hide password toggles.
 *
 * The eye button next to a password field flips the input's `type` between
 * "password" and "text", so the user can check what they typed. The contract is
 * a `[data-password-toggle]` button inside the same .input-group as the input it
 * controls (see templates/pwms/login.html).
 *
 * Like the theme toggle, the control is revealed only once this script has run
 * (see the `password-toggle-ready` rules in style.css): without JS it could not
 * change anything, so it stays hidden rather than sitting there inert.
 */
(function () {
    "use strict";

    var SELECTOR = "[data-password-toggle]";

    function inputFor(button) {
        var group = button.closest(".input-group");
        return group ? group.querySelector("input") : null;
    }

    function setShown(button, input, shown) {
        input.type = shown ? "text" : "password";
        button.setAttribute("aria-pressed", shown ? "true" : "false");
        var label = shown ? "Hide password" : "Show password";
        button.setAttribute("aria-label", label);
        button.setAttribute("title", label);
    }

    function bind() {
        Array.prototype.forEach.call(document.querySelectorAll(SELECTOR), function (button) {
            var input = inputFor(button);
            if (!input || input.dataset.passwordToggleBound === "true") {
                return;
            }
            setShown(button, input, false);
            button.addEventListener("click", function () {
                var shown = input.type === "password";
                setShown(button, input, shown);
                // The user is mid-typing, so put the caret back in the field
                // instead of leaving focus on the button.
                input.focus();
            });
            input.dataset.passwordToggleBound = "true";
        });
        document.documentElement.classList.add("password-toggle-ready");
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bind);
    } else {
        bind();
    }
})();
