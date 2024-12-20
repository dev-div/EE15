# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
import requests
from urllib.parse import quote
from werkzeug.urls import url_join

from odoo import models, fields, tools, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class SocialLivePostLinkedin(models.Model):
    _inherit = 'social.live.post'

    linkedin_post_id = fields.Char('Actual LinkedIn ID of the post')

    def _refresh_statistics(self):
        super(SocialLivePostLinkedin, self)._refresh_statistics()
        accounts = self.env['social.account'].search([('media_type', '=', 'linkedin')])

        for account in accounts:
            linkedin_post_ids = self.env['social.live.post'].sudo().search(
                [('account_id', '=', account.id), ('linkedin_post_id', '!=', False)],
                order='create_date DESC', limit=1000
            )
            if not linkedin_post_ids:
                continue

            linkedin_post_ids = {post.linkedin_post_id: post for post in linkedin_post_ids}

            session = requests.Session()

            # The LinkedIn API limit the query parameters to 4KB
            # An LinkedIn URN is approximatively 40 characters
            # So we keep a big margin and we split over 50 LinkedIn posts
            for batch_linkedin_post_ids in tools.split_every(50, linkedin_post_ids):
                endpoint = url_join(
                    self.env['social.media']._LINKEDIN_ENDPOINT,
                    'socialMetadata?ids=List(%s)' % ','.join(map(quote, batch_linkedin_post_ids)))

                response = session.get(endpoint, headers=account._linkedin_bearer_headers(), timeout=10)

                if not response.ok or 'results' not in response.json():
                    account._action_disconnect_accounts(response.json())
                    _logger.error('Error when fetching LinkedIn stats: %r.', response.text)
                    break

                for urn, stats in response.json()['results'].items():
                    if not urn or not stats or urn not in batch_linkedin_post_ids:
                        continue

                    like_count = sum(like.get('count', 0) for like in stats.get('reactionSummaries', {}).values())
                    comment_count = stats.get('commentSummary', {}).get('count', 0)
                    linkedin_post_ids[urn].update({'engagement': like_count + comment_count})

    def _post(self):
        linkedin_live_posts = self.filtered(lambda post: post.account_id.media_type == 'linkedin')
        super(SocialLivePostLinkedin, (self - linkedin_live_posts))._post()

        linkedin_live_posts._post_linkedin()

    def _post_linkedin(self):
        for live_post in self:
            url_in_message = self.env['social.post']._extract_url_from_message(live_post.message)

            data = {
                "author": live_post.account_id.linkedin_account_urn,
                "commentary": live_post.message,
                "distribution": {"feedDistribution": "MAIN_FEED"},
                "lifecycleState": "PUBLISHED",
                "visibility": "PUBLIC",
            }

            if live_post.post_id.image_ids:
                try:
                    images_urn = [
                        self._linkedin_upload_image(live_post.account_id, image_id)
                        for image_id in live_post.post_id.image_ids
                    ]
                except UserError as e:
                    live_post.write({
                        'state': 'failed',
                        'failure_reason': e.name
                    })
                    continue

                if len(images_urn) == 1:
                    data["content"] = {"media": {"id": images_urn[0]}}
                else:
                    data["content"] = {
                        "multiImage": {
                            "images": [{"id": image_urn} for image_urn in images_urn],
                        }
                    }

            elif url_in_message:
                data["content"] = {"article": {"source": url_in_message, "title": url_in_message}}

            response = requests.post(
                url_join(self.env['social.media']._LINKEDIN_ENDPOINT, 'posts'),
                headers=live_post.account_id._linkedin_bearer_headers(),
                json=data, timeout=10)

            post_id = response.headers.get('x-restli-id')
            if response.ok and post_id:
                values = {
                    'state': 'posted',
                    'failure_reason': False,
                    'linkedin_post_id': post_id,
                }
            else:
                try:
                    response_json = response.json()
                except Exception:
                    response_json = {}
                values = {
                    'state': 'failed',
                    'failure_reason': response_json.get('message', _('unknown')),
                }

                if response_json.get('serviceErrorCode') == 65600:
                    # Invalid access token
                    self.account_id._action_disconnect_accounts(response)

            live_post.write(values)

    def _linkedin_upload_image(self, account_id, image_id):
        # 1 - Register your image to be uploaded
        data = {
            "initializeUploadRequest": {
                "owner": account_id.linkedin_account_urn,
            },
        }
        response = requests.post(
                url_join(self.env['social.media']._LINKEDIN_ENDPOINT, 'images?action=initializeUpload'),
                headers=account_id._linkedin_bearer_headers(),
                json=data, timeout=10)

        if not response.ok:
            _logger.error('Could not upload the image: %r.', response.text)

        response = response.json()
        if 'value' not in response or 'uploadUrl' not in response['value']:
            raise UserError(_("We could not upload your image, try reducing its size and posting it again (error: Failed during upload registering)."))

        # 2 - Upload image binary file
        upload_url = response['value']['uploadUrl']
        image_urn = response['value']['image']

        data = image_id.with_context(bin_size=False).raw

        headers = account_id._linkedin_bearer_headers()
        headers['Content-Type'] = 'application/octet-stream'

        response = requests.request('POST', upload_url, data=data, headers=headers, timeout=15)

        if not response.ok:
            raise UserError(_("We could not upload your image, try reducing its size and posting it again."))

        return image_urn
