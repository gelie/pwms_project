/**
 * Report page behaviour: keep the address bar in step with the filter form and
 * run the share modal over fetch so the generated link can be shown in place.
 *
 * Everything is bound to `document` (event delegation) because the preview —
 * including the share modal — is replaced wholesale by HTMX whenever a filter
 * changes. A listener on the modal element itself would be thrown away with it.
 */
((document) => {
  "use strict";

  const FILTER_FORM_ID = "report-filters";
  const SHARE_FORM_ID = "report-share-form";
  const SHARE_RESULT_ID = "report-share-result";

  // -- address bar ----------------------------------------------------------
  function syncUrl(form) {
    const params = new URLSearchParams(new FormData(form));
    // Empty fields are noise in the URL; the server treats them as "unset".
    for (const key of Array.from(params.keys())) {
      if (!params.get(key)) {
        params.delete(key);
      }
    }
    const query = params.toString();
    const url = form.action + (query ? `?${query}` : "");
    window.history.replaceState({}, "", url);
  }

  // -- share modal ----------------------------------------------------------
  function resultBox(form) {
    return form.querySelector(`#${SHARE_RESULT_ID}`);
  }

  function resetResult(box) {
    box.classList.add("d-none");
    box.classList.remove("alert", "alert-success", "alert-danger");
    box.replaceChildren();
  }

  function showError(box, payload) {
    const messages = [];
    if (payload && payload.error) {
      messages.push(payload.error);
    }
    if (payload && payload.errors) {
      for (const field of Object.values(payload.errors)) {
        for (const entry of field) {
          messages.push(entry.message || String(entry));
        }
      }
    }
    if (!messages.length) {
      messages.push("The share could not be created.");
    }
    const list = document.createElement("ul");
    list.className = "mb-0";
    for (const message of messages) {
      const item = document.createElement("li");
      item.textContent = message;
      list.append(item);
    }
    box.append(list);
    box.classList.add("alert", "alert-danger");
    box.classList.remove("d-none");
  }

  function showLink(box, payload) {
    const intro = document.createElement("p");
    intro.className = "mb-2 small";
    intro.textContent = payload.sent
      ? `Link created and emailed to the recipients (${payload.sent}).`
      : "Link created. Copy it and send it on yourself.";
    box.append(intro);

    // A repeating share says so, and when it next goes out.
    if (payload.schedule && payload.schedule !== "none") {
      const repeat = document.createElement("p");
      repeat.className = "mb-2 small";
      repeat.textContent = payload.next_send_at
        ? `Repeats ${payload.schedule}; next send ${new Date(payload.next_send_at).toLocaleString()}.`
        : `Repeats ${payload.schedule}.`;
      box.append(repeat);
    }

    const row = document.createElement("div");
    row.className = "input-group";

    const input = document.createElement("input");
    input.type = "text";
    input.className = "form-control";
    input.value = payload.link || "";
    input.readOnly = true;
    input.setAttribute("aria-label", "Shareable report link");

    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-outline-secondary";
    button.textContent = "Copy";
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(input.value);
        button.textContent = "Copied";
        window.setTimeout(() => (button.textContent = "Copy"), 2000);
      } catch {
        input.select();
        document.execCommand("copy");
      }
    });

    row.append(input, button);
    box.append(row);

    if (payload.expires_at) {
      const expiry = document.createElement("p");
      expiry.className = "mt-2 mb-0 small text-body-secondary";
      expiry.textContent = `This link expires on ${new Date(payload.expires_at).toLocaleString()}.`;
      box.append(expiry);
    }

    box.classList.add("alert", "alert-success");
    box.classList.remove("d-none");
  }

  async function submitShare(form) {
    const box = resultBox(form);
    if (!box) {
      return;
    }
    resetResult(box);

    const submit = form.querySelector('button[type="submit"]');
    if (submit) {
      submit.disabled = true;
    }

    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: {
          Accept: "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.success) {
        showError(box, payload);
        return;
      }
      showLink(box, payload);
    } catch (error) {
      showError(box, { error: `The share could not be created: ${error}` });
    } finally {
      if (submit) {
        submit.disabled = false;
      }
    }
  }

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    if (form.id === FILTER_FORM_ID) {
      syncUrl(form);
    } else if (form.id === SHARE_FORM_ID) {
      event.preventDefault();
      submitShare(form);
    }
  });
})(document);
