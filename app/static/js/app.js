"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  const mobileMenu = $("#mobileMenu");
  const sidebar = $("#sidebar");
  mobileMenu?.addEventListener("click", () => sidebar?.classList.toggle("open"));
  document.addEventListener("click", (event) => {
    if (window.innerWidth <= 980 && sidebar?.classList.contains("open") &&
        !sidebar.contains(event.target) && !mobileMenu?.contains(event.target)) sidebar.classList.remove("open");
  });

  $$('[data-open-dialog]').forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.openDialog);
      if (dialog?.showModal) dialog.showModal();
    });
  });
  $$('[data-close-dialog]').forEach((button) => button.addEventListener("click", () => button.closest("dialog")?.close()));
  $$('dialog').forEach((dialog) => dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  }));

  const productState = $("#productState");
  productState?.addEventListener("change", () => {
    const url = new URL(window.location.href);
    url.searchParams.set("state", productState.value);
    window.location.assign(url.toString());
  });

  $$('[data-confirm]').forEach((form) => form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  }));

  const flashClose = $(".flash-close");
  flashClose?.addEventListener("click", () => flashClose.closest(".flash")?.remove());

  const togglePassword = $("#togglePassword");
  togglePassword?.addEventListener("click", () => {
    const input = $("#password");
    if (!input) return;
    input.type = input.type === "password" ? "text" : "password";
    togglePassword.textContent = input.type === "password" ? "Show" : "Hide";
  });

  const quantityInput = $("#quantity");
  const productSelect = $("#product_id");
  const subtotal = $("#orderSubtotal");
  const stockHint = $("#stockHint");
  const updateOrderEstimate = () => {
    if (!quantityInput || !productSelect) return;
    const option = productSelect.selectedOptions[0];
    const price = Number(option?.dataset.price || 0);
    const stock = Number(option?.dataset.stock || 0);
    const quantity = Math.max(0, Number(quantityInput.value || 0));
    if (subtotal) subtotal.textContent = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(price * quantity);
    if (stockHint) {
      stockHint.textContent = option?.value ? `${stock} unit${stock === 1 ? "" : "s"} available · max 100 per order` : "Select an item to check availability.";
      stockHint.style.color = option?.value && quantity > stock ? "#c65364" : "";
    }
  };
  productSelect?.addEventListener("change", updateOrderEstimate);
  quantityInput?.addEventListener("input", updateOrderEstimate);
  $$('[data-quantity-change]').forEach((button) => button.addEventListener("click", () => {
    if (!quantityInput) return;
    const min = Number(quantityInput.min || 1);
    const max = Number(quantityInput.max || 100);
    quantityInput.value = String(Math.min(max, Math.max(min, Number(quantityInput.value || min) + Number(button.dataset.quantityChange))));
    updateOrderEstimate();
  }));
  updateOrderEstimate();
})();
