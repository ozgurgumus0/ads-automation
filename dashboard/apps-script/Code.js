// Serves the Creative Performance dashboard as a web app.
// The page loads its data from the Google Sheets in the viewer's browser.
function doGet() {
  return HtmlService.createHtmlOutputFromFile('Index')
    .setTitle('Creative Performance')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}
