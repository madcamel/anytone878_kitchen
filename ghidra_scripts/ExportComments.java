// Write every user comment (EOL/PRE/POST/PLATE/REPEATABLE) to a file: "addr TYPE text" (newlines as \n).
// args: <out.txt>
//@category Ham
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.*;
import java.io.PrintWriter;

public class ExportComments extends GhidraScript {
	@Override
	public void run() throws Exception {
		Listing L = currentProgram.getListing();
		String[] names = {"EOL", "PRE", "POST", "PLATE", "REPEATABLE"};
		int[] types = {CodeUnit.EOL_COMMENT, CodeUnit.PRE_COMMENT, CodeUnit.POST_COMMENT, CodeUnit.PLATE_COMMENT,
			CodeUnit.REPEATABLE_COMMENT};
		int n = 0;
		try (PrintWriter w = new PrintWriter(getScriptArgs()[0])) {
			for (Address a : L.getCommentAddressIterator(currentProgram.getMemory(), true)) {
				for (int i = 0; i < types.length; i++) {
					String c = L.getComment(types[i], a);
					if (c != null) { w.println(a + " " + names[i] + " " + c.replace("\r", "").replace("\n", "\\n")); n++; }
				}
			}
		}
		println("comments exported: " + n);
	}
}
