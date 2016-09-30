// See http://zulip.readthedocs.io/en/latest/pointer.html for notes on
// how this system is designed.

var pointer = (function () {

var exports = {};

exports.recenter_pointer_on_display = false;

// Toggles re-centering the pointer in the window
// when Home is next clicked by the user
exports.suppress_scroll_pointer_update = false;
exports.furthest_read = -1;
exports.server_furthest_read = -1;

var pointer_update_in_flight = false;

function update_pointer() {
    if (!pointer_update_in_flight) {
        pointer_update_in_flight = true;
        return channel.put({
            url:      '/json/users/me/pointer',
            idempotent: true,
            data:     {pointer: pointer.furthest_read},
            success: function () {
                pointer.server_furthest_read = pointer.furthest_read;
                pointer_update_in_flight = false;
            },
            error: function () {
                pointer_update_in_flight = false;
            }
        });
    } else {
        // Return an empty, resolved Deferred.
        return $.when();
    }
}


exports.send_pointer_update = function () {
    // Only bother if you've read new messages.
    if (pointer.furthest_read > pointer.server_furthest_read) {
        update_pointer();
    }
};

function unconditionally_send_pointer_update() {
    if (pointer_update_in_flight) {
        // Keep trying.
        var deferred = $.Deferred();

        setTimeout(function () {
            deferred.resolve(unconditionally_send_pointer_update());
        }, 100);
        return deferred;
    } else {
        return update_pointer();
    }
}

exports.fast_forward_pointer = function () {
    channel.get({
        url: '/json/users/me',
        idempotent: true,
        data: {email: page_params.email},
        success: function (data) {
            unread.mark_all_as_read(function () {
                pointer.furthest_read = data.max_message_id;
                unconditionally_send_pointer_update().then(function () {
                    ui.change_tab_to('#home');
                    reload.initiate({immediate: true,
                                     save_pointer: false,
                                     save_narrow: false,
                                     save_compose: true});
                });
            });
        }
    });
};

exports.keep_pointer_in_view = function () {
    // See viewport.recenter_view() for related logic to keep the pointer onscreen.
    // This function mostly comes into place for mouse scrollers, and it
    // keeps the pointer in view.  For people who purely scroll with the
    // mouse, the pointer is kind of meaningless to them, but keyboard
    // users will occasionally do big mouse scrolls, so this gives them
    // a pointer reasonably close to the middle of the screen.
    var candidate;
    var next_row = current_msg_list.selected_row();

    if (next_row.length === 0) {
        return;
    }

    var info = viewport.message_viewport_info();
    var top_threshold = info.visible_top + (1/10 * info.visible_height);
    var bottom_threshold = info.visible_top + (9/10 * info.visible_height);

    function message_is_far_enough_down() {
        if (viewport.at_top()) {
            return true;
        }

        var message_top = next_row.offset().top;

        // If the message starts after the very top of the screen, we just
        // leave it alone.  This avoids bugs like #1608, where overzealousness
        // about repositioning the pointer can cause users to miss messages.
        if (message_top >= info.visible_top) {
            return true;
        }


        // If at least part of the message is below top_threshold (10% from
        // the top), then we also leave it alone.
        var bottom_offset = message_top + next_row.outerHeight(true);
        if (bottom_offset >= top_threshold) {
            return true;
        }

        // If we got this far, the message is not "in view."
        return false;
    }

    function message_is_far_enough_up() {
        return viewport.at_bottom() ||
            (next_row.offset().top <= bottom_threshold);
    }

    function adjust(in_view, get_next_row) {
        // return true only if we make an actual adjustment, so
        // that we know to short circuit the other direction
        if (in_view(next_row)) {
            return false;  // try other side
        }
        while (!in_view(next_row)) {
            candidate = get_next_row(next_row);
            if (candidate.length === 0) {
                break;
            }
            next_row = candidate;
        }
        return true;
    }

    if (!adjust(message_is_far_enough_down, rows.next_visible)) {
        adjust(message_is_far_enough_up, rows.prev_visible);
    }

    current_msg_list.select_id(rows.id(next_row), {from_scroll: true});
};

return exports;
}());
if (typeof module !== 'undefined') {
    module.exports = pointer;
}
