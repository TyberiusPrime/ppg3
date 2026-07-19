# ppg3

Greenfield implementation of a new pipeline system as successor to pypipegraph2.

It's got a bunch of good ideas, and somewhat working impls (sandboxing, web based watcher,
decent tracebacks),  and I think it shows that the 'build-and-discover' workflow
with multiple stores can work - even though we don't have truly remote stores yet.

But the whole model isn't defined rigorously,
and the ux from the 'write some code side' is awful (views/outputs/exports, one 
output symlink folder for everything, no way to mix multiple scripts etc).

I need to think some more about this before attempting this again.
