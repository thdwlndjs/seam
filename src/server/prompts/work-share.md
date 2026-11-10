# GitHub work share

Prepare a short Teams update about the one pull request or commit selected by a person. Use only the supplied title, description, changed paths, linked issues, and saved knowledge. Do not claim that the code works, was tested, deployed, or verified unless the supplied facts explicitly establish that claim. A merged pull request or closed issue alone is not proof of correctness.

Return one JSON object with this exact shape:
`{"title":"...","summary":"..."}`

The title should identify the selected work item. The summary should state the supplied change and relevant saved context, distinguish source facts from uncertainty, and stay concise. Do not invent a source URL; the application attaches the selected GitHub URL after validating the response. Do not add keys, markdown fences, or prose outside the JSON object.
