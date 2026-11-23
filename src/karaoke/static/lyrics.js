// Karaoke lyrics rendering of the player.
// Lines are shown with a word wipe driven by `getTime()` (audio time in seconds); aligned
// words with several syllables are wiped syllable by syllable.

const LYRICS_LEAD = 0.15;     // show a line slightly before it starts
const WORD_LEAD = 0.1;        // start the word/syllable wipe slightly early
const LINE_LINGER = 0.5;      // keep a finished line visible this long
const GAP_THRESHOLD = 3.5;    // pauses longer than this clear the line
const COUNTDOWN_SECONDS = 3;

function lyricsStatusText(data) {
    if (data.aligned) return 'Word-synced lyrics';
    if (data.aligning) return 'Line-synced lyrics, aligning words...';
    if (data.align_error) return 'Line-synced lyrics (word alignment failed)';
    return data.synced ? 'Line-synced lyrics' : 'Lyrics found, but not synced';
}

function createLyricsView({ current, next, countdown, getTime }) {
    let segments = [];
    let offset = 0;
    let shownCurrent = null;
    let shownNext = null;
    let wipes = [];  // {span, start, end} per word or syllable

    function renderCurrent(segment) {
        if (segment === shownCurrent) return;
        shownCurrent = segment;
        current.replaceChildren();
        wipes = [];
        if (!segment) return;

        segment.words.forEach((word, i) => {
            for (const piece of word.syllables || [{ text: word.word, start: word.start, end: word.end }]) {
                const span = document.createElement('span');
                span.className = 'lyric-word';
                span.textContent = piece.text;
                wipes.push({ span, start: piece.start, end: piece.end });
                current.append(span);
            }
            if (i < segment.words.length - 1) current.append(' ');
        });
    }

    function renderNext(segment) {
        if (segment === shownNext) return;
        shownNext = segment;
        next.textContent = segment ? segment.text : '';
    }

    function frame() {
        if (!segments.length) return;
        const time = getTime() - offset;

        let index = -1;
        while (index + 1 < segments.length && segments[index + 1].start - LYRICS_LEAD <= time) {
            index++;
        }

        let line = segments[index] || null;
        const nextLine = segments[index + 1] || null;

        // Clear a finished line during long pauses and count down to the next one.
        if (line && time > line.end + LINE_LINGER &&
            (!nextLine || nextLine.start - line.end >= GAP_THRESHOLD)) {
            line = null;
        }
        const untilNext = nextLine ? nextLine.start - time : Infinity;
        const waiting = !line && untilNext > 0;

        countdown.hidden = !(waiting && untilNext <= COUNTDOWN_SECONDS);
        countdown.textContent = Math.ceil(untilNext);

        renderCurrent(line);
        renderNext(!waiting || untilNext <= COUNTDOWN_SECONDS + 2 ? nextLine : null);

        if (line) {
            for (const { span, start, end } of wipes) {
                const duration = Math.max(end - start, 0.05);
                const progress = Math.min(1, Math.max(0, (time + WORD_LEAD - start) / duration));
                span.style.setProperty('--p', progress);
            }
        }
    }

    function loop() {
        frame();
        requestAnimationFrame(loop);
    }
    requestAnimationFrame(loop);

    return {
        setLyrics(data) {
            segments = data?.segments || [];
            shownCurrent = shownNext = undefined;
            renderCurrent(null);
            renderNext(null);
            countdown.hidden = true;
        },
        setOffset(value) {
            offset = value;
        },
        frame,
    };
}
