function BookmarkBar() {
    var me = this;
    me.modesToScan = ['lsb', 'usb', 'cw', 'am', 'sam', 'nfm'];
    me.localBookmarks = new BookmarkLocalStorage();
    me.$container = $('#openwebrx-bookmarks-container');
    me.bookmarks = {};

    me.$container.on('click', '.bookmark', function(e){
        var $bookmark = $(e.target).closest('.bookmark');
        me.$container.find('.bookmark').removeClass('selected');
        if (UI.tuneBookmark($bookmark.data())) {
            $bookmark.addClass('selected');
            UI.toggleScanner(false);
        }
    });

    me.$container.on('click', '.action[data-action=edit]', function(e){
        e.stopPropagation();
        var $bookmark = $(e.target).closest('.bookmark');
        me.showEditDialog($bookmark.data());
    });

    me.$container.on('click', '.action[data-action=delete]', function(e){
        e.stopPropagation();
        var $bookmark = $(e.target).closest('.bookmark');
        me.localBookmarks.deleteBookmark($bookmark.data());
        me.loadLocalBookmarks();
    });

    var $bookmarkButton = $('#openwebrx-panel-receiver').find('.openwebrx-bookmark-button');
    if (typeof(Storage) !== 'undefined') {
        $bookmarkButton.show();
    } else {
        $bookmarkButton.hide();
    }
    $bookmarkButton.click(function(){
        me.showEditDialog();
    });

    me.$dialog = $('#openwebrx-dialog-bookmark');
    me.$dialog.find('.openwebrx-button[data-action=cancel]').click(function(){
        me.$dialog.hide();
    });
    me.$dialog.find('.openwebrx-button[data-action=submit]').click(function(){
        me.storeBookmark();
    });
    me.$dialog.find('form').on('submit', function(e){
        e.preventDefault();
        me.storeBookmark();
    });
}

BookmarkBar.prototype.position = function () {
    var waterfallWidth = $('body').width();
    var range = get_visible_freq_range();
    $('#openwebrx-bookmarks-container').find('.bookmark').each(function () {
        const px = scale_px_from_freq($(this).data('frequency'), range);
        const visible = px >= -32 && px <= waterfallWidth + 32;
        $(this).toggle(visible);
        if (visible) {
            $(this).css('left', px);
        }
    });
};

BookmarkBar.prototype.loadLocalBookmarks = function(){
    var bwh = bandwidth / 2;
    var start = center_freq - bwh;
    var end = center_freq + bwh;
    var bookmarks = this.localBookmarks.getBookmarks().filter(function(b){
        return b.frequency >= start && b.frequency <= end;
    });
    this.replace_bookmarks(bookmarks, 'local', true);
};

BookmarkBar.prototype.replace_bookmarks = function(bookmarks, source, editable) {
    editable = !!editable;
    bookmarks = bookmarks.map(function(b){
        b.source = source;
        b.editable = editable;
        return b;
    });
    this.bookmarks[source] = bookmarks;
    this.render();
};

BookmarkBar.prototype.render = function(){
    var bookmarks = Object.values(this.bookmarks).reduce(function(l, v){ return l.concat(v); });
    bookmarks = bookmarks.sort(function(a, b){ return a.frequency - b.frequency; });
    var elements = bookmarks.map(function(b){
        var $bookmark = $(
            '<div class="bookmark" data-source="' + b.source + '"' + (b.editable?' editable="editable"':'') + '>' +
                '<div class="bookmark-actions">' +
                    '<div class="openwebrx-button action" data-action="edit"><svg viewBox="0 0 80 80"><use xlink:href="static/gfx/svg-defs.svg#edit"></use></svg></div>' +
                    '<div class="openwebrx-button action" data-action="delete"><svg viewBox="0 0 80 80"><use xlink:href="static/gfx/svg-defs.svg#trashcan"></use></svg></div>' +
                '</div>' +
                '<div class="bookmark-content">' + b.name + '</div>' +
            '</div>'
        );
        if (b.description) {
            $bookmark.prop('title', b.description);
        }
        $bookmark.data(b);
        return $bookmark;
    });
    this.$container.find('.bookmark').remove();
    this.$container.append(elements);
	this.position();
};

BookmarkBar.prototype.showEditDialog = function(bookmark) {
    if (!bookmark) {
        var freq  = UI.getFrequency();
        var mode1 = UI.getModulation();
        var mode2 = UI.getUnderlying();
        if (!!mode1 && !!mode2) {
            // check for default underlying demod
            var m = Modes.findByModulation(mode1);
            if (m && m.underlying.indexOf(mode2) == 0) mode2 = '';
        }
        bookmark = {
            name        : '',
            frequency   : mode1 === 'cw'? freq + 800 : freq,
            modulation  : mode1,
            underlying  : mode2,
            description : '',
            scannable   : this.modesToScan.indexOf(mode1) >= 0
        }
    }
    this.$dialog.bookmarkDialog().setValues(bookmark);
    this.$dialog.show();
    this.$dialog.find('#name').focus();
};

BookmarkBar.prototype.sanitizeBookmark = function(b) {
    // must have name, frequency, and modulation
    if (!b.name || !b.frequency || !b.modulation)
        return "Must have name, frequency, and modulation.";

    // must have non-empty name
    b.name = b.name.trim();
    if (b.name.length <= 0) return "Must have a non-empty name.";

    // must have positive frequency
    b.frequency = Number(b.frequency);
    if (b.frequency <= 0) return "Frequency must be positive.";

    // must have valid modulation
    var mode = Modes.findByModulation(b.modulation);
    if (!mode) return "Must have valid modulation."

    // check that underlying demodulator is valid
    if (!b.underlying)
        b.underlying = '';
    else if (!mode.underlying)
        return "Must not have underlying modulation.";
    else if (mode.underlying.indexOf(b.underlying) < 0)
        return "Must have valid underlying modulation.";

    return null;
};

BookmarkBar.prototype.storeBookmark = function() {
    var me = this;
    var bookmark = this.$dialog.bookmarkDialog().getValues();
    if (!bookmark) return;

    var error = this.sanitizeBookmark(bookmark);
    if (error) { alert(error); return; }

    var bookmarks = me.localBookmarks.getBookmarks();

    if (!bookmark.id) {
        if (bookmarks.length) {
            bookmark.id = 1 + Math.max.apply(Math, bookmarks.map(function(b){ return b.id || 0; }));
        } else {
            bookmark.id = 1;
        }
    }

    bookmarks = bookmarks.filter(function(b) { return b.id !== bookmark.id; });
    bookmarks.push(bookmark);

    me.localBookmarks.setBookmarks(bookmarks);
    me.loadLocalBookmarks();
    me.$dialog.hide();
};

BookmarkBar.prototype.getAllBookmarks = function() {
    var sb = this.bookmarks['server'];
    var lb = this.bookmarks['local'];
    return !sb.length? (!lb.length? [] : lb) : !lb.length? sb : sb.concat(lb);
};
