package org.jellyfin.androidtv.ui.playback.overlay.action

import android.content.Context
import android.view.View
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.ui.everflix.EfPicker
import org.jellyfin.androidtv.constant.getQualityProfiles
import org.jellyfin.androidtv.preference.UserPreferences
import org.jellyfin.androidtv.ui.playback.PlaybackController
import org.jellyfin.androidtv.ui.playback.VideoQualityController
import org.jellyfin.androidtv.ui.playback.overlay.CustomPlaybackTransportControlGlue
import org.jellyfin.androidtv.ui.playback.overlay.VideoPlayerAdapter

class SelectQualityAction(
	context: Context,
	customPlaybackTransportControlGlue: CustomPlaybackTransportControlGlue,
	userPreferences: UserPreferences
) : CustomAction(context, customPlaybackTransportControlGlue) {
	private val previousQualitySelection = userPreferences[UserPreferences.maxBitrate]
	private val qualityController = VideoQualityController(previousQualitySelection, userPreferences)
	private val qualityProfiles = getQualityProfiles(context)
	private var popup: EfPicker? = null

	init {
		initializeWithIcon(R.drawable.ic_select_quality)
	}

	override fun handleClickAction(
		playbackController: PlaybackController,
		videoPlayerAdapter: VideoPlayerAdapter,
		context: Context,
		view: View,
	) {
		videoPlayerAdapter.leanbackOverlayFragment.setFading(false)
		dismissPopup()
		popup = EfPicker(
			context, "Calidad",
			qualityProfiles.entries.map { (k, v) -> EfPicker.Option(v, k == qualityController.currentQuality) },
			onPick = { i ->
				qualityController.currentQuality = qualityProfiles.keys.elementAt(i)
				playbackController.refreshStream()
			},
			onDismiss = {
				videoPlayerAdapter.leanbackOverlayFragment.setFading(true)
				popup = null
			},
		)
		popup?.show()
	}

	fun dismissPopup() {
		popup?.dismiss()
	}
}
