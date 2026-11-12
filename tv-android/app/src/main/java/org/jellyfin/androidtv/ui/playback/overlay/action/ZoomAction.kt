package org.jellyfin.androidtv.ui.playback.overlay.action

import android.content.Context
import android.view.View
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.ui.everflix.EfPicker
import org.jellyfin.androidtv.preference.constant.ZoomMode
import org.jellyfin.androidtv.ui.playback.PlaybackController
import org.jellyfin.androidtv.ui.playback.overlay.CustomPlaybackTransportControlGlue
import org.jellyfin.androidtv.ui.playback.overlay.VideoPlayerAdapter

class ZoomAction(
	context: Context,
	customPlaybackTransportControlGlue: CustomPlaybackTransportControlGlue,
) : CustomAction(context, customPlaybackTransportControlGlue) {
	private var popup: EfPicker? = null

	init {
		initializeWithIcon(R.drawable.ic_aspect_ratio)
	}

	override fun handleClickAction(
		playbackController: PlaybackController,
		videoPlayerAdapter: VideoPlayerAdapter,
		context: Context,
		view: View,
	) {
		videoPlayerAdapter.leanbackOverlayFragment.setFading(false)
		dismissPopup()
		val modes = listOf(ZoomMode.FIT to R.string.lbl_fit, ZoomMode.AUTO_CROP to R.string.lbl_auto_crop, ZoomMode.STRETCH to R.string.lbl_stretch)
		popup = EfPicker(
			context, "Imagen",
			modes.map { (mode, label) -> EfPicker.Option(context.getString(label), playbackController.zoomMode == mode) },
			onPick = { i -> playbackController.setZoom(modes[i].first) },
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
