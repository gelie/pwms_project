"use strict";

/**
 * Clear the bill document URL when the B-number changes.
 *
 * The B-number names the bill's SharePoint folder. A document URL chosen from
 * the previous folder would point at the wrong place after a renumbering, so
 * the field is reset as soon as the number changes. The server also refuses to
 * use the picker while an edit's new B-number is unsaved, and BillForm rejects
 * a save that changes the B-number while a document URL is still set.
 */
(function () {
    const number = document.getElementById("id_bill_number");
    const documentUrl = document.getElementById("id_bill_document_url");
    if (!number || !documentUrl) {
        return;
    }

    let previous = number.value;
    number.addEventListener("input", function () {
        if (number.value === previous) {
            return;
        }
        previous = number.value;
        documentUrl.value = "";
    });
})();
