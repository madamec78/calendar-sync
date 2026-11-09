// Progressive enhancement only: every page works without JavaScript.
document.addEventListener("submit", function (event) {
  var form = event.target;
  var message = form.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
    return;
  }
  var busy = form.getAttribute("data-busy");
  if (busy) {
    var submitter = event.submitter;
    // Disable only after the browser has built the form data (a disabled submitter is not sent).
    setTimeout(function () {
      form.querySelectorAll("button").forEach(function (button) {
        if (submitter === button) { button.textContent = busy; }
        button.disabled = true;
      });
    }, 0);
  }
});
