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

    // Deep link on load, then keep the hash in step with the open tab.
    show(tabFor(window.location.hash));

    tabs.forEach(function (tab) {
        tab.addEventListener("shown.bs.tab", function (event) {
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
