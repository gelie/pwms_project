/**
 * Keep the workflow detail page's open tab in the URL hash.
 *
 * Bootstrap's tab component already switches panes; this adds the two things it
 * does not do, so a detail page survives a refresh (and a tab can be linked):
 *
 *   - open the tab named by `location.hash` on load;
 *   - mirror the tab a reader opens back into the hash.
 *
 * `replaceState` is used rather than `pushState` so switching tabs does not fill
 * the back button with tab changes.
 */
(function () {
    "use strict";

    var tabList = document.getElementById("workflow-tabs");
    if (!tabList || typeof bootstrap === "undefined") {
        return;
    }

    var tabs = Array.prototype.slice.call(
        tabList.querySelectorAll('[data-bs-toggle="tab"]')
    );
    if (!tabs.length) {
        return;
    }

    /**
     * The tab whose pane matches `hash`, matched on the attribute rather than
     * through a CSS selector built from the URL (which would let a crafted hash
     * reach into the selector).
     */
    function tabFor(hash) {
        if (!hash) {
            return null;
        }
        for (var i = 0; i < tabs.length; i += 1) {
            if (tabs[i].getAttribute("data-bs-target") === hash) {
                return tabs[i];
            }
        }
        return null;
    }

    function show(tab) {
        if (tab) {
            bootstrap.Tab.getOrCreateInstance(tab).show();
        }
    }

    /**
     * md and smaller: a dropdown replaces the tab strip. The visible tab list
     * is hidden there, but its buttons still drive the panes; the dropdown just
     * forwards clicks to them. Items are rebuilt each time the menu opens so the
     * counters (which HTMX refreshes in place) are never stale.
     */
    var menu = document.getElementById("workflow-tabs-dropdown-menu");
    var toggle = document.getElementById("workflow-tabs-dropdown-toggle");
    var toggleLabel = toggle && toggle.querySelector(".workflow-tabs-dropdown-label");

    function labelOf(tab) {
        var span = tab.querySelector("span");
        return span ? span.textContent.trim() : tab.textContent.trim();
    }

    function countOf(tab) {
        var count = tab.querySelector(".workflow-tab-count");
        return count ? count.textContent.trim() : "";
    }

    function syncToggle(tab) {
        if (!toggleLabel || !tab) {
            return;
        }
        toggleLabel.textContent = labelOf(tab);
    }

    function buildMenu() {
        menu.textContent = "";
        tabs.forEach(function (tab) {
            var item = document.createElement("li");
            var button = document.createElement("button");
            button.type = "button";
            button.className =
                "dropdown-item d-flex align-items-center gap-2" +
                (tab.classList.contains("active") ? " active" : "");

            var icon = tab.querySelector("svg");
            if (icon) {
                button.appendChild(icon.cloneNode(true));
            }
            var text = document.createElement("span");
            text.textContent = labelOf(tab);
            button.appendChild(text);

            var count = countOf(tab);
            if (count) {
                var badge = document.createElement("span");
                badge.className = "workflow-tab-count ms-auto";
                badge.textContent = count;
                button.appendChild(badge);
            }

            button.addEventListener("click", function () {
                show(tab);
            });
            item.appendChild(button);
            menu.appendChild(item);
        });
    }

    if (menu && toggle) {
        toggle.addEventListener("show.bs.dropdown", buildMenu);
        syncToggle(
            tabs.filter(function (tab) {
                return tab.classList.contains("active");
            })[0]
        );
    }

    // Deep link on load, then keep the hash in step with the open tab.
    show(tabFor(window.location.hash));

    tabs.forEach(function (tab) {
        tab.addEventListener("shown.bs.tab", function (event) {
            syncToggle(event.target);
            var pane = event.target.getAttribute("data-bs-target");
            if (pane && window.location.hash !== pane) {
                window.history.replaceState(null, "", pane);
            }
        });
    });

    // Someone pasting a different #tab into the address bar is a hash-only
    // navigation, so no load event fires and the tab has to be switched here.
    window.addEventListener("hashchange", function () {
        show(tabFor(window.location.hash));
    });
})();
