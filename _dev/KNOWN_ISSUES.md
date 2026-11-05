KNOWN ISSUES

===================================
this will not be fixed/implemented
===================================

# issue 1
- refresh compare after restart
- i will not refresh compare after restart because it s slow if there is a lot of compare of big files, and because of this problem too https://github.com/Alexey-T/CudaText/issues/6398

# issue 2
- make ui paint on viewport only
- i will not do it because paint is no longuer a problem, it s fast, and we cannot paint everything on on_scroll on viweport, like gaps, they must be painted from start to end at once otherwise calculations will be wrong

# issue 3
- add collapsing of unchanged regions, so review of a huge file with three small hunks does not means scrolling through everything 
- this is overkill, i m not motivated to implement it for now, i never saw a diff app do it and i don t think it will be usefull, i never needed to do it, but i m open to change my position if multiple users ask for it

# issue 4
- rewrite everything to do like Beyound Compare:
    from Beyound Compare docs: Standard alignment (default) — a proprietary divide-and-conquer scheme that aligns "by comparing successively smaller sections of each file. Parts of the alignment can be shown before the entire comparison is finished" alignment — i.e. it is incremental/streaming, chosen so huge files paint progressively.
    https://sparkles-docs.pages.dev/research/diff-review/beyond-compare.html
- i did a lot of work in the plugin and native pascal code, progressive compare is a comletely diferent method and may need to rewrite everything again, and the native pascal algo are so fast currently specially Myers algo

# issue 5
- the plugin does not clean entries from cuda_differ2_state.json of sessions whose file no longer exists, this means the json file will keep growing for ever if user do compares and delete the session file where the compare happened before closing the diff tab
- i intentionally keep it like that because "Doesn't exist now" legitimately happens to sessions that aren't dead: removable media / network shares not yet mounted; portable or cloud-synced settings folders (i deliberately made session paths relative "for portability" - and in a synced settings dir, the state JSON and the session files arrive on independent schedules, so a startup check easily runs before the session file has synced down); a file deleted by accident and restored from backup or re-created by saving the same session name again; stat() failing on perms / long Windows paths / case differences.
so the plugin deletes only what it can PROVE dead. "File not on disk right now" is not proof , it's a flaky snapshot , and a wrong deletion is unrecoverable while a stale entry is free.
- the only safe solution is to add a new command "Clean stale session entries" command, so the plugin show to the user all stale entries and he decide to remove them or not, TODO? i don t know yet, i need to think more about it









