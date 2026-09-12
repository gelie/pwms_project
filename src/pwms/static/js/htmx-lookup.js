"use strict";

/**
 * HTMX-driven searchable pickers.
 *
 * A widget (see templates/pwms/_lookup_field.html) is a [data-lookup]
 * wrapper with three parts: the real form field rendered hidden, a text input
 * that runs the search, and the container HTMX swaps results into.
 *
 * The results fragment calls window.selectOption(pk, label) - or
 * window.selectGroup / window.selectUser for the group and user fragments -
 * which writes the chosen pk back into the hidden field so an ordinary form POST
 * submits it. Typing after a choice clears that value again, otherwise a stale
 * pk could be saved together with a label the user has since edited.
 *
 * Documented in docs/Search Lookups.md.
 */
(function () {
    const WRAPPER = "[data-lookup]";
    const TEXT_INPUT = 'input[type="text"]';
    const HIDDEN_INPUT = 'input[type="hidden"]';
    const RESULTS = ".lookup-results";

    // The widget the user is working in. Results are a shared fragment, so the
    // click handler needs to know which widget to write the value back to.
    let active = null;

    /**
     * The [data-lookup] wrapper an event happened in: both the picker's own
     * controls and the results HTMX swaps into the wrapper are inside it.
     * @param {EventTarget|null} element @returns {Element|null}
     */
    function wrapperFor(element) {
        if (!(element instanceof Element)) {
            return null;
        }
        return element.closest(WRAPPER);
    }

    /** @param {Element} wrapper @param {boolean} open */
    function setOpen(wrapper, open) {
        wrapper.querySelector(RESULTS).classList.toggle("d-none", !open);
        wrapper.querySelector(TEXT_INPUT).setAttribute("aria-expanded", String(open));
    }

    /**
     * Pickers named in ``data-clears`` follow this one (city follows country),
     * so their stored pk and label are dropped as soon as this value changes.
     * @param {Element} wrapper
     */
    function resetDependents(wrapper) {
        (wrapper.dataset.clears || "")
            .split(",")
            .map(function (selector) {
                return selector.trim();
            })
            .filter(Boolean)
            .forEach(function (selector) {
                const dependent = document.querySelector(selector);
                if (!dependent) {
                    return;
                }
                dependent.querySelector(HIDDEN_INPUT).value = "";
                dependent.querySelector(TEXT_INPUT).value = "";
                setOpen(dependent, false);
            });
    }

    /** @param {string} value @param {string} label */
    function select(value, label) {
        const wrapper = active || document.querySelector(WRAPPER);
        if (!wrapper) {
            return;
        }
        wrapper.querySelector(HIDDEN_INPUT).value = value;
        wrapper.querySelector(TEXT_INPUT).value = label;
        setOpen(wrapper, false);
        resetDependents(wrapper);
    }

    // Called by the country/city results fragments.
    window.selectOption = function (value, label) {
        select(value, label);
    };

    // Called by the group results fragment.
    window.selectGroup = function (value, label) {
        select(value, label);
    };

    // Called by the user results fragment: users are labelled by name, with the
    // username as the fallback for accounts that have no name set.
    window.selectUser = function (value, username, fullName) {
        select(value, (fullName || "").trim() || username);
    };

    document.addEventListener("focusin", function (event) {
        const wrapper = wrapperFor(event.target);
        if (wrapper) {
            active = wrapper;
        }
    });

    // Typing invalidates the previous choice: the label no longer describes the
    // pk still sitting in the hidden field (nor anything picked from it).
    document.addEventListener("input", function (event) {
        if (event.target instanceof HTMLInputElement && event.target.type === "text") {
            const wrapper = wrapperFor(event.target);
            if (wrapper) {
                wrapper.querySelector(HIDDEN_INPUT).value = "";
                resetDependents(wrapper);
            }
        }
    });

    // The results only exist once HTMX has filled the container.
    document.addEventListener("htmx:afterSwap", function (event) {
        const wrapper = wrapperFor(event.target);
        if (wrapper) {
            setOpen(wrapper, true);
        }
    });

    document.addEventListener("click", function (event) {
        document.querySelectorAll(WRAPPER).forEach(function (wrapper) {
            if (!wrapper.contains(event.target)) {
                setOpen(wrapper, false);
            }
        });
    });
})();
