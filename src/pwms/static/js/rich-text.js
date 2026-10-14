"use strict";

/**
 * A small, dependency-free rich-text editor.
 *
 * Any ``<textarea data-rich-text>`` on the page is replaced, in place, by a
 * toolbar plus a ``contenteditable`` surface. The textarea is kept (hidden) and
 * holds the HTML, so an ordinary form POST still submits ``name=...`` with the
 * formatted value — the editor is a façade over the field, not a new one.
 *
 * The editor vocabulary is deliberately small (bold / italic / underline /
 * headings / lists / quote / link) to match the server-side allowlist in
 * ``pwms/utils/richtext.py``; anything a browser could produce beyond that is
 * dropped when the value is displayed. Paste is inserted as plain text so no
 * foreign markup can slip in through the clipboard.
 *
 * Uses ``document.execCommand``: deprecated, but the one rich-text primitive
 * every current browser ships, and the reason this needs no vendored library.
 */
(function () {
    var EDITOR = "textarea[data-rich-text]";
    var TOOLBAR = "rich-text-toolbar";
    var SURFACE = "rich-text-editor";
    var SOURCE = "rich-text-source";

    // label, command, argument — enough for a meeting agenda.
    var BUTTONS = [
        { label: "B", title: "Bold", cmd: "bold", css: "fw-bold" },
        { label: "I", title: "Italic", cmd: "italic", css: "fst-italic" },
        { label: "U", title: "Underline", cmd: "underline", css: "text-decoration-underline" },
        { label: "H2", title: "Heading", cmd: "formatBlock", arg: "h2" },
        { label: "H3", title: "Sub-heading", cmd: "formatBlock", arg: "h3" },
        { label: "• List", title: "Bulleted list", cmd: "insertUnorderedList" },
        { label: "1. List", title: "Numbered list", cmd: "insertOrderedList" },
        { label: "❝", title: "Quote", cmd: "formatBlock", arg: "blockquote" },
        { label: "Link", title: "Add link", action: "link" },
        { label: "Unlink", title: "Remove link", cmd: "unlink" },
        { label: "Clear", title: "Clear formatting", cmd: "removeFormat" },
    ];

    function safeUrl(value) {
        if (!value) {
            return "";
        }
        var url = value.trim();
        if (url.charAt(0) === "/" || url.charAt(0) === "#") {
            return url;
        }
        if (/^(https?:|mailto:)/i.test(url)) {
            return url;
        }
        return "";
    }

    function makeButton(spec) {
        var button = document.createElement("button");
        button.type = "button";
        button.title = spec.title;
        button.setAttribute("aria-label", spec.title);
        button.textContent = spec.label;
        if (spec.css) {
            button.className = spec.css;
        }
        if (spec.cmd) {
            button.dataset.cmd = spec.cmd;
            if (spec.arg) {
                button.dataset.arg = spec.arg;
            }
        } else if (spec.action) {
            button.dataset.action = spec.action;
        }
        return button;
    }

    function runCommand(editor, button) {
        var cmd = button.dataset.cmd;
        if (!cmd) {
            return;
        }
        editor.focus();
        // styleWithCSS off keeps the output tag-based (<b>, <h2>), which is what
        // the server allowlist expects.
        document.execCommand("styleWithCSS", false, false);
        document.execCommand(cmd, false, button.dataset.arg || null);
    }

    function addLink(editor) {
        var url = safeUrl(window.prompt("Link address (https://…, mailto:…, /pwms/… or #anchor)"));
        if (!url) {
            return;
        }
        editor.focus();
        document.execCommand("createLink", false, url);
    }

    function build(textarea) {
        if (textarea.dataset.richTextReady === "1") {
            return;
        }
        textarea.dataset.richTextReady = "1";

        var root = document.createElement("div");
        root.className = "rich-text";

        var toolbar = document.createElement("div");
        toolbar.className = TOOLBAR;
        toolbar.setAttribute("role", "toolbar");

        var editor = document.createElement("div");
        editor.className = SURFACE;
        editor.contentEditable = "true";
        editor.setAttribute("role", "textbox");
        editor.setAttribute("aria-multiline", "true");
        editor.innerHTML = textarea.value;

        BUTTONS.forEach(function (spec) {
            toolbar.appendChild(makeButton(spec));
        });

        textarea.classList.add(SOURCE);
        textarea.setAttribute("aria-hidden", "true");
        textarea.setAttribute("tabindex", "-1");
        textarea.required = false;

        textarea.parentNode.insertBefore(root, textarea);
        root.appendChild(toolbar);
        root.appendChild(editor);
        root.appendChild(textarea);

        function sync() {
            textarea.value = editor.innerHTML;
        }

        editor.addEventListener("input", sync);
        editor.addEventListener("blur", sync);

        // Keep the caret/selection in the editor when a toolbar button is pressed.
        toolbar.addEventListener("mousedown", function (event) {
            event.preventDefault();
        });
        toolbar.addEventListener("click", function (event) {
            var target = event.target;
            if (!(target instanceof Element)) {
                return;
            }
            var button = target.closest("[data-cmd], [data-action]");
            if (!button) {
                return;
            }
            event.preventDefault();
            if (button.dataset.action === "link") {
                addLink(editor);
            } else {
                runCommand(editor, button);
            }
            sync();
        });

        // Paste as plain text: nothing from a foreign page can carry markup in.
        editor.addEventListener("paste", function (event) {
            event.preventDefault();
            var clipboard = event.clipboardData || window.clipboardData;
            var text = clipboard ? clipboard.getData("text/plain") : "";
            document.execCommand("insertText", false, text);
            sync();
        });

        if (textarea.form) {
            textarea.form.addEventListener("submit", sync);
            // HTMX intercepts the submit, so sync again just before it serialises
            // the form (the click that triggered it has already blurred us).
            textarea.form.addEventListener("htmx:beforeRequest", sync);
        }
    }

    function initWithin(node) {
        var scope = node && node.querySelectorAll ? node : document;
        if (node && node.matches && node.matches(EDITOR)) {
            build(node);
        }
        scope.querySelectorAll(EDITOR).forEach(build);
    }

    function focusEditor(node) {
        if (!node) {
            return;
        }
        var editor =
            node.classList && node.classList.contains(SURFACE)
                ? node
                : node.querySelector && node.querySelector("." + SURFACE);
        if (editor) {
            editor.focus();
        }
    }

    function start() {
        initWithin(document);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }

    // HTMX swaps content into the page (the inline agenda editor); build any
    // editor the new content brought, and put the caret in it.
    if (window.htmx && typeof window.htmx.onLoad === "function") {
        window.htmx.onLoad(function (content) {
            initWithin(content);
            focusEditor(content);
        });
    }
})();
