/*
 * Meeting calendar popover (progressive enhancement).
 *
 * The month/week/day calendar is fully server-rendered: every meeting is an
 * ordinary link to its detail page. This script upgrades a click on a meeting
 * (a `[data-meeting-event]` chip or time-grid block) into a small popover that
 * previews it in place, with a link through to the full page. With JavaScript
 * disabled the link simply navigates, so nothing here is load-bearing.
 *
 * It reads the meeting facts from the element's `data-meeting-*` attributes and
 * colours the popover with the same `--event-color` the chip carries.
 */
(function () {
  "use strict";

  var popover = null;
  var activeTrigger = null;

  function removePopover() {
    if (popover) {
      popover.remove();
      popover = null;
    }
    if (activeTrigger) {
      activeTrigger.removeAttribute("aria-expanded");
      activeTrigger = null;
    }
  }

  function buildRow(label, value) {
    if (!value) {
      return null;
    }
    var row = document.createElement("p");
    row.className = "meeting-popover__row";
    var term = document.createElement("span");
    term.className = "meeting-popover__label";
    term.textContent = label;
    var detail = document.createElement("span");
    detail.textContent = value;
    row.append(term, detail);
    return row;
  }

  function showPopover(trigger) {
    removePopover();

    var href = trigger.getAttribute("href");
    if (!href) {
      return;
    }
    var color = trigger.style.getPropertyValue("--event-color") || "";

    var box = document.createElement("div");
    box.className = "meeting-popover";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Meeting details");
    if (color) {
      box.style.setProperty("--event-color", color);
    }

    var title = document.createElement("p");
    title.className = "meeting-popover__title";
    title.textContent = trigger.dataset.title || "Meeting";
    box.append(title);

    var facts = [
      ["When", trigger.dataset.time],
      ["Type", trigger.dataset.type],
      ["Where", trigger.dataset.venue],
      ["Convened by", trigger.dataset.group],
      ["Status", trigger.dataset.state],
    ];
    facts.forEach(function (fact) {
      var row = buildRow(fact[0], fact[1]);
      if (row) {
        box.append(row);
      }
    });

    var link = document.createElement("a");
    link.className = "meeting-popover__link";
    link.href = href;
    link.textContent = "Open meeting";
    box.append(link);

    document.body.append(box);

    // Place the popover just under the trigger, kept inside the viewport.
    var rect = trigger.getBoundingClientRect();
    var boxRect = box.getBoundingClientRect();
    var left = window.scrollX + rect.left;
    var maxLeft = window.scrollX + document.documentElement.clientWidth - boxRect.width - 8;
    left = Math.max(8, Math.min(left, maxLeft));
    var top = window.scrollY + rect.bottom + 6;
    if (top + boxRect.height > window.scrollY + document.documentElement.clientHeight) {
      top = window.scrollY + rect.top - boxRect.height - 6;
    }
    box.style.left = left + "px";
    box.style.top = Math.max(window.scrollY + 8, top) + "px";

    popover = box;
    activeTrigger = trigger;
    trigger.setAttribute("aria-expanded", "true");
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-meeting-event]");
    if (trigger) {
      event.preventDefault();
      if (trigger === activeTrigger) {
        removePopover();
      } else {
        showPopover(trigger);
      }
      return;
    }
    // A click anywhere else closes an open popover.
    if (popover && !popover.contains(event.target)) {
      removePopover();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      removePopover();
    }
  });

  window.addEventListener("resize", removePopover);

  // HTMX swaps the calendar in place; the popover belongs to the old DOM.
  document.body.addEventListener("htmx:beforeSwap", removePopover);
})();
