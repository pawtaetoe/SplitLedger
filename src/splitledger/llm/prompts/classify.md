Read this chat message to an expense bot and decide what it wants.
intent is log when the user is recording money they spent.
intent is ask for a question about their own spending, budgets or who owes them, or whether a price or purchase is reasonable or affordable, even when it mentions an amount.
intent is upcoming when they tell you about a bill or purchase they expect later or that repeats, like rent every month or a present for a birthday next week, or want to see or remove those. Something they already paid is log.
intent is plan when they want to set up, see or change their budget plan, not when they ask how they are doing against it.
intent is split when they want to share a bill with other people. Set ways only when they want the total shared equally, to the number of people counting themselves. If they split by dish or item, or say who had what, ways is null even when they give a group size. When intent is split also set split_mode: even when they want the total shared equally, including a head count alone like split 4 ways, items when they split by dish or item, say who had what, or just say split, unsure only when the words contradict each other, else null.
intent is claim when they say which items on a shared bill they or other people had, like I had the laksa or Anna and Ben shared the calamari.
intent is chat for greetings, thanks or small talk.
intent is unclear when you cannot tell.
The message may be in any language, slang or shorthand.
When intent is log, fill expense as described below. Otherwise expense is null.
