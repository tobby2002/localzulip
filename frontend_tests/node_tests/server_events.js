var assert = require('assert');
var _ = global._;

add_dependencies({
    util: 'js/util.js',
    tutorial: 'js/tutorial.js'
});

var noop = function () {};

set_global('document', {});
set_global('window', {
    addEventListener: noop
});

global.stub_out_jquery();

set_global('blueslip', {});
set_global('channel', {});
set_global('home_msg_list', {
    select_id: noop,
    selected_id: function () {return 1;}
});
set_global('page_params', {test_suite: false});
set_global('reload', {
    is_in_progress: function () {return false;}
});

var page_params = global.page_params;

set_global('echo', {
    process_from_server: function (messages) {
        return messages;
    },
    set_realm_filters: noop
});

var server_events = require('js/server_events.js');

// Start blueslip tests here

var setup = function (results) {
    server_events.home_view_loaded();
    set_global('message_store', {
        insert_new_messages: function () {
            throw Error('insert error');
        },
        update_messages: function () {
            throw Error('update error');
        }
    });
    set_global('subs', {
        update_subscription_properties: function () {
            throw Error('subs update error');
        }
    });
    global.blueslip.error = function (msg, more_info, stack) {
        results.msg = msg;
        results.more_info = more_info;
        results.stack = stack;
    };
    global.blueslip.exception_msg = function (ex) {
        return ex.message;
    };
};

(function test_event_dispatch_error() {
    var results = {};
    setup(results);

    var data = {events: [{type: 'stream', op: 'update', id: 1, other: 'thing'}]};
    global.channel.post = function (options) {
        options.success(data);
    };

    server_events.restart_get_events();

    assert.equal(results.msg, 'Failed to process an event\n' +
                              'subs update error');
    assert.equal(results.more_info.event.type , 'stream');
    assert.equal(results.more_info.event.op , 'update');
    assert.equal(results.more_info.event.id , 1);
    assert.equal(results.more_info.other , undefined);
}());


(function test_event_new_message_error() {
    var results = {};
    setup(results);

    var data = {events: [{type: 'message', id: 1, other: 'thing', message: {}}]};
    global.channel.post = function (options) {
        options.success(data);
    };

    server_events.restart_get_events();

    assert.equal(results.msg, 'Failed to insert new messages\n' +
                               'insert error');
    assert.equal(results.more_info, undefined);
}());

(function test_event_edit_message_error() {
    var results = {};
    setup(results);

    var data = {events: [{type: 'update_message', id: 1, other: 'thing'}]};
    global.channel.post = function (options) {
        options.success(data);
    };

    server_events.restart_get_events();

    assert.equal(results.msg, 'Failed to update messages\n' +
                              'update error');
    assert.equal(results.more_info, undefined);
}());
