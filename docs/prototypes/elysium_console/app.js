(() => {
  "use strict";

  const titles = {
    home: ["SUNDAY · 22:42", "此刻"],
    "live-ready": ["VOICE LIVE", "实时通话"],
    "live-call": ["VOICE LIVE", "和爱莉通话中"],
    unavailable: ["CAPABILITY", "功能暂不可用"],
  };

  const views = Array.from(document.querySelectorAll("[data-view]"));
  const routeLinks = Array.from(document.querySelectorAll("[data-route]"));
  const pageEyebrow = document.querySelector("#page-eyebrow");
  const pageTitle = document.querySelector("#page-title");
  const workspace = document.querySelector("#workspace");
  const toastRoot = document.querySelector("[data-toast-root]");
  let toastTimer = 0;

  const normalizeRoute = (value) => {
    const requested = String(value || "").replace(/^#/, "");
    return Object.hasOwn(titles, requested) ? requested : "home";
  };

  const showRoute = (route, { focus = false } = {}) => {
    const target = normalizeRoute(route);
    views.forEach((view) => {
      const visible = view.dataset.view === target;
      view.hidden = !visible;
      view.classList.toggle("is-visible", visible);
    });

    routeLinks.forEach((link) => {
      const active = link.dataset.route === target ||
        (target === "live-call" && link.dataset.route === "live-ready");
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });

    const [eyebrow, title] = titles[target];
    pageEyebrow.textContent = eyebrow;
    pageTitle.textContent = title;
    document.title = `${title} · Elysium Console Prototype`;

    const callMode = target === "live-call";
    document.body.classList.toggle("is-call-mode", callMode);
    if (focus) workspace.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const navigate = (route) => {
    const targetHash = `#${normalizeRoute(route)}`;
    if (window.location.hash === targetHash) showRoute(route, { focus: true });
    else window.location.hash = targetHash;
  };

  const showToast = (message) => {
    window.clearTimeout(toastTimer);
    toastRoot.textContent = message;
    toastRoot.hidden = false;
    toastTimer = window.setTimeout(() => {
      toastRoot.hidden = true;
    }, 3200);
  };

  document.addEventListener("click", (event) => {
    const go = event.target.closest("[data-go]");
    if (go) {
      navigate(go.dataset.go);
      return;
    }

    const toast = event.target.closest("[data-toast]");
    if (toast) {
      showToast(toast.dataset.toast);
      return;
    }

    const fixToggle = event.target.closest("[data-toggle-fix]");
    if (fixToggle) {
      const card = document.querySelector("[data-fix-card]");
      card.hidden = !card.hidden;
      fixToggle.textContent = card.hidden ? "怎么处理" : "收起说明";
      return;
    }

    const diagnosticsToggle = event.target.closest("[data-toggle-diagnostics]");
    if (diagnosticsToggle) {
      const panel = document.querySelector("[data-diagnostics]");
      panel.hidden = !panel.hidden;
      document.querySelectorAll("[data-toggle-diagnostics]").forEach((button) => {
        button.setAttribute("aria-expanded", String(!panel.hidden));
      });
      if (!panel.hidden) panel.querySelector("button")?.focus();
      return;
    }

    const muteToggle = event.target.closest("[data-toggle-mute]");
    if (muteToggle) {
      const muted = muteToggle.getAttribute("aria-pressed") !== "true";
      muteToggle.setAttribute("aria-pressed", String(muted));
      muteToggle.querySelector("small").textContent = muted ? "已静音" : "静音";
      showToast(muted ? "麦克风已静音。" : "麦克风已恢复。");
      return;
    }
  });

  window.addEventListener("hashchange", () => showRoute(window.location.hash, { focus: true }));
  showRoute(window.location.hash);
})();
