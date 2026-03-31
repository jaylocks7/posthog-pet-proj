Start Time: 6:01 pm PT

Assignment Approach:

-The main data we have at our disposal from a Github Repo alone is the following:
1. commits
2. pull requests
3. issues

So to determine the Top 5 Most Impactful Engineers at PostHog, we have to extract insights from these data points

And to narrow down what I believe impactful to be I consider:
-what kinds of code changes an engineer is making (not just quantity)? all documentation? or fixes week after week?
-

-----
1. 
Looking at the commit history, I see that commit messages are formatted with the task type at
the beginning:

Examples
-feat: sandbox dev environment v2 (env cleanup)
-chore(code): add worktree config for posthog code
-refactor: use outputs for hog transformations
-revert(flags): remove sent_at delta metric for client-to-server transit time
-fix: disable egress proxy in LLM gateway client
-test(experiments): add feature flag for DW A/A test
-docs(internal): Document parallel query execution pattern in feature flags service

Obviously number of commits is not a good metric to determine impact by (since you can game that
with 100 tiny docs() commits)

The next part of a commit I considered were the code changes themselves. But for the sake of the 1.5 hr time limit (and thinking about all the approaches to determining high impact code like sentiment analysis, etc) I decided to scope that out.

So what I settled on extracting from commit history was what TYPES of commits engineers pushed.

I argue that feat, fix, test are higher impact commit types than refactor, revert, chore, docs. The reason being is that feat, fix, test are what keeps the product rolling out new features and bug-free (as much as possible) whereas refactor, revert, chore, docs aren't primary drivers
for product success.

Thus, from commit history, on a user basis, from the last 90 days, I want to track user commit history by category and weigh feat, fix, test commits more than refactor, revert, chore, docs commits.

----

2. Looking at PRs, the same scoping will apply as for commit history. For the sake of brevity I'm going to focus on number of comments by PR type (same as commit type).

----

3. Looking at issues, 

