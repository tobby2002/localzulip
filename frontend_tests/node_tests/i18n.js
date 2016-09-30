add_dependencies({
    Handlebars: 'handlebars',
    templates: 'js/templates',
    i18n: 'i18next'
});

var i18n = global.i18n;
i18n.init({
    nsSeparator: false,
    keySeparator: false,
    interpolation: {
        prefix: "__",
        suffix: "__"
    },
    lng: 'fr',
    resources: {
        fr: {
            translation: {
                'Reply': "French",
                "You'll receive notifications when a message arrives and __page_params.product_name__ isn't in focus or the message is offscreen.": "Some French text with __page_params.product_name__"
            }
        }
    }
});

var jsdom = require("jsdom");
var window = jsdom.jsdom().defaultView;
global.$ = require('jquery')(window);
var _ = global._;

// When writing these tests, the following command might be helpful:
// ./tools/get-handlebar-vars static/templates/*.handlebars

function render(template_name, args) {
    global.use_template(template_name);
    return global.templates.render(template_name, args);
}

(function test_t_tag() {
    var args = {
        "message": {
            is_stream: true,
            id: "99",
            stream: "devel",
            subject: "testing",
            sender_full_name: "King Lear"
        },
        "can_edit_message": true,
        "can_mute_topic": true,
        "narrowed": true
    };

    var html = '<div style="height: 250px">';
    html += render('actions_popover_content', args);
    html += "</div>";
    var link = $(html).find("a.respond_button");
    assert.equal(link.text().trim(), 'French');
    global.write_test_output("actions_popover_content.handlebars", html);
}());

(function test_tr_tag() {
    var args = {
        "page_params": {
            "fullname": "John Doe",
            "product_name": "Zulip",
            "password_auth_enabled": false,
            "avatar_url": "http://example.com",
            "left_side_userlist": false,
            "twenty_four_hour_time": false,
            "stream_desktop_notifications_enabled": false,
            "stream_sounds_enabled": false,
            "desktop_notifications_enabled": false,
            "sounds_enabled": false,
            "enable_offline_email_notifications": false,
            "enable_offline_push_notifications": false,
            "enable_digest_emails": false,
            "domain": "zulip.com",
            "autoscroll_forever": false,
            "default_desktop_notifications": false
        }
    };

    var html = render('settings_tab', args);
    var div = $(html).find("div.notification-reminder");
    assert.equal(div.text().trim(), 'Some French text with Zulip');
    global.write_test_output("test_tr_tag settings", html);
}());
