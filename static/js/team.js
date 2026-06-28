/**
 * EventHub — Team Up: Background Invite Polling
 * Polls /invites/pending every 5 seconds to detect new invites and show popups.
 * Included on all pages via base.html so users never miss an invite.
 */

(function () {
    "use strict";

    const POLL_INTERVAL = 5000; // 5 seconds
    const POPUP_DURATION = 5000; // 5 seconds
    let knownInviteIds = new Set();
    let popupContainer = null;
    let isFirstPoll = true;

    function init() {
        // Create popup container
        popupContainer = document.createElement("div");
        popupContainer.className = "eh-invite-popup-container";
        popupContainer.id = "ehInvitePopupContainer";
        document.body.appendChild(popupContainer);

        // Start polling
        pollInvites();
        setInterval(pollInvites, POLL_INTERVAL);
    }

    async function pollInvites() {
        try {
            const resp = await fetch("/invites/pending?format=json", {
                headers: { Accept: "application/json" },
                credentials: "same-origin",
            });
            if (!resp.ok) return;

            const data = await resp.json();
            const invites = data.invites || [];
            const count = data.count || 0;

            // Update navbar badge
            updateBadge(count);

            // Detect new invites (skip on first poll to avoid flooding)
            if (!isFirstPoll) {
                for (const inv of invites) {
                    if (!knownInviteIds.has(inv.id)) {
                        showPopup(inv);
                    }
                }
            }

            // Update known IDs
            knownInviteIds = new Set(invites.map((i) => i.id));
            isFirstPoll = false;
        } catch (e) {
            // Silently ignore fetch errors (user might not be logged in)
        }
    }

    function updateBadge(count) {
        const badge = document.getElementById("ehInviteBadge");
        if (badge) {
            badge.textContent = count > 0 ? count : "";
        }
    }

    function showPopup(invite) {
        const popup = document.createElement("div");
        popup.className = "eh-invite-popup";
        popup.id = `ehPopup_${invite.id}`;
        popup.innerHTML = `
            <div class="eh-popup-header">
                <span class="eh-popup-label"><i class="bi bi-people-fill me-1"></i>Team Invite</span>
                <button class="eh-popup-dismiss" data-dismiss="${invite.id}" aria-label="Dismiss">
                    <i class="bi bi-x-lg"></i>
                </button>
            </div>
            <div class="eh-popup-event">${escapeHtml(invite.event_title)}</div>
            <div class="eh-popup-from">
                <i class="bi bi-person-fill me-1"></i>Invited by <strong>${escapeHtml(invite.leader_name || invite.leader_email)}</strong>
            </div>
            <div class="eh-popup-actions">
                <button class="btn btn-accept-invite btn-sm" data-action="accepted" data-invite-id="${invite.id}">
                    <i class="bi bi-check-lg me-1"></i>Accept
                </button>
                <button class="btn btn-decline-invite btn-sm" data-action="declined" data-invite-id="${invite.id}">
                    <i class="bi bi-x-lg me-1"></i>Decline
                </button>
            </div>
        `;

        popupContainer.appendChild(popup);

        // Event listeners
        popup.querySelector("[data-dismiss]").addEventListener("click", () => {
            dismissPopup(popup);
        });

        popup.querySelectorAll("[data-action]").forEach((btn) => {
            btn.addEventListener("click", async (e) => {
                const action = e.currentTarget.dataset.action;
                const inviteId = e.currentTarget.dataset.inviteId;
                await respondToInvite(inviteId, action);
                dismissPopup(popup);
                pollInvites(); // Refresh immediately
            });
        });

        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            if (popup.parentNode) {
                dismissPopup(popup);
            }
        }, POPUP_DURATION);
    }

    function dismissPopup(popup) {
        popup.classList.add("eh-popup-exit");
        setTimeout(() => {
            if (popup.parentNode) {
                popup.parentNode.removeChild(popup);
            }
        }, 300);
    }

    async function respondToInvite(inviteId, response) {
        try {
            await fetch(`/invite/${inviteId}/respond`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    Accept: "application/json",
                },
                credentials: "same-origin",
                body: JSON.stringify({ response }),
            });
        } catch (e) {
            console.error("Failed to respond to invite:", e);
        }
    }

    function escapeHtml(str) {
        if (!str) return "";
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    // Initialize when DOM is ready
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
