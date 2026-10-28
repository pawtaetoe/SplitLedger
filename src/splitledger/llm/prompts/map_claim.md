Read a chat message about a shared bill and say who had which items.
The items are numbered. Use only those numbers.
For each person list the items they had. Use name null for the sender, even when they say I or me.
Use the names as written in the message. If the message gives a group size but not names, call the others Friend 1, Friend 2 and so on.
When several people shared an item, list it for each of them with portion null.
Set portion only when someone says they had part of an item, like half the beer is 0.5 or a third is 0.33.
When the message says everyone had their own of something, give each person one of those items.
If you cannot match the message to the items, return no people.
