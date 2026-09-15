"use strict";

/**
 * Add-list rows for Django formsets (the delegates and resolutions lists on the
 * delegation report form).
 *
 * A [data-adder-group] holds three things: the formset's management form, a
 * one-line [data-adder] row of controls, and the [data-adder-items] list. Each
 * item in that list carries the formset row for one entry (submitted, visually
 * hidden) plus the card the user sees, and a <template data-adder-empty> holds
 * the empty form for cloning.
 *
 * "Add" reads the adder's controls by form field name, clones the template with
 * the next index, fills the row and the card from what was entered, appends it
 * and clears the adder again. "Remove" ticks the row's DELETE box and hides the
 * item, which Django reads as "do not create this one" (or "delete this one" for
 * an entry that already exists). The row is hidden rather than removed so the
 * remaining indices stay contiguous for the management form.
 */
(function () {
    const GROUP = "[data-adder-group]";
    const ADDER = "[data-adder]";
    const ITEMS = "[data-adder-items]";
    const EMPTY = "template[data-adder-empty]";
    const ADD = "[data-adder-add]";
    const REMOVE = "[data-adder-remove]";
    const SLOT = "data-adder-slot";
    const PLACEHOLDER = "__prefix__";

    /** The formset field name an input belongs to: "formset-3-title" -> "title". */
    function fieldName(input) {
        return input.name ? input.name.split("-").pop() : "";
    }

    /** The group an event target sits in, or null. */
    function groupFor(target) {
        return target instanceof Element ? target.closest(GROUP) : null;
    }

    /**
     * What the adder currently holds, keyed by formset field name.
     *
     * A lookup's own input is hidden and holds the pk, so the label the picker
     * put in its search box is captured separately as "search".
     */
    function adderValues(group) {
        const values = {};
        group.querySelectorAll(`${ADDER} [name]`).forEach(function (input) {
            if (input.type === "hidden") {
                const search = document.getElementById(`${input.id}_search`);
                values[fieldName(input)] = input.value;
                if (search) {
                    values.search = search.value;
                }
            } else {
                values[fieldName(input)] = input.value;
            }
        });
        return values;
    }

    /** True when the list already holds ``value`` for the given field. */
    function listHas(group, field, value) {
        const items = group.querySelector(ITEMS);
        if (!items || !value) {
            return false;
        }
        return Array.from(
            items.querySelectorAll(`[data-adder-item]:not(.d-none) [name$="-${field}"]`)
        ).some(function (input) {
            return input.value === value;
        });
    }

    /** Show the group's problem message (see data-adder-duplicate). */
    function warn(group, message) {
        const box = group.querySelector("[data-adder-warning]");
        if (box) {
            box.textContent = message;
            box.classList.remove("d-none");
        }
    }

    function clearWarning(group) {
        const box = group.querySelector("[data-adder-warning]");
        if (box) {
            box.textContent = "";
            box.classList.add("d-none");
        }
    }

    /** Put the adder back to blank so the next entry starts fresh. */
    function clearAdder(group) {
        group.querySelectorAll(`${ADDER} [name]`).forEach(function (input) {
            if (input._flatpickr) {
                // Clears the picker's own state as well as the input's value.
                input._flatpickr.clear();
            } else if (input.tagName === "SELECT") {
                input.selectedIndex = 0;
            } else {
                input.value = "";
            }
        });
    }

    function addItem(group) {
        const template = group.querySelector(EMPTY);
        const total = group.querySelector('input[name$="-TOTAL_FORMS"]');
        const items = group.querySelector(ITEMS);
        if (!template || !total || !items) {
            return;
        }
        const values = adderValues(group);
        // A group that names a unique field refuses the same value twice, so the
        // server never has to reject the entry afterwards.
        const unique = group.dataset.adderUnique;
        if (unique && listHas(group, unique, values[unique])) {
            const label = values.search || values[unique] || "That entry";
            warn(
                group,
                `${label} ${group.dataset.adderDuplicate || "is already on the list."}`
            );
            return;
        }
        clearWarning(group);
        const index = parseInt(total.value, 10);
        const holder = document.createElement("div");
        holder.innerHTML = template.innerHTML.replaceAll(PLACEHOLDER, String(index));
        const item = holder.firstElementChild;
        if (!item) {
            return;
        }

        // The submitted row: copy each control's value in by field name.
        item.querySelectorAll("[name]").forEach(function (input) {
            if (input.type === "checkbox") {
                return;
            }
            input.value = values[fieldName(input)] || "";
        });

        // The card the user reads, including its initial.
        item.querySelectorAll(`[${SLOT}]`).forEach(function (slot) {
            slot.textContent = values[slot.dataset.adderSlot] || "";
        });
        const avatar = item.querySelector("[data-adder-avatar]");
        if (avatar) {
            avatar.textContent = (values.search || "").trim().charAt(0).toUpperCase();
        }

        items.appendChild(item);
        total.value = index + 1;
        clearAdder(group);
    }

    function removeItem(item) {
        if (!item) {
            return;
        }
        const deleted = item.querySelector('input[name$="-DELETE"]');
        if (deleted) {
            deleted.checked = true;
        }
        item.classList.add("d-none");
    }

    document.addEventListener("click", function (event) {
        const target = event.target;
        if (!(target instanceof Element)) {
            return;
        }
        const group = groupFor(target);
        if (!group) {
            return;
        }
        if (target.closest(ADD)) {
            event.preventDefault();
            addItem(group);
        } else if (target.closest(REMOVE)) {
            event.preventDefault();
            removeItem(target.closest("[data-adder-item]"));
        }
    });
})();
