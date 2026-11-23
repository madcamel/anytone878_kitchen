// Make the program's functions exactly match annotations/functions.txt ("entry s-e[,s-e...]"):
// removes functions not listed (only auto-named ones; user-named extras are reported, kept),
// and (re)creates every listed function whose body differs, with the recorded body. Run after
// auto-analysis so two collaborators get identical function boundaries. Idempotent.
// args: <functions.txt>
//@category Ham
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.SourceType;
import java.math.BigInteger;
import java.nio.file.*;
import java.util.*;

public class ApplyFunctions extends GhidraScript {
	@Override
	public void run() throws Exception {
		FunctionManager fm = currentProgram.getFunctionManager();
		Register tmode = currentProgram.getRegister("TMode");
		Map<Address, AddressSet> want = new LinkedHashMap<>();
		for (String line : Files.readAllLines(Paths.get(getScriptArgs()[0]))) {
			line = line.replaceAll("#.*", "").trim();
			if (line.isEmpty()) continue;
			String[] f = line.split("\\s+");
			AddressSet body = new AddressSet();
			for (String r : f[1].split(",")) {
				String[] ab = r.split("-");
				body.add(toAddr(Long.parseLong(ab[0], 16)), toAddr(Long.parseLong(ab[1], 16)));
			}
			want.put(toAddr(Long.parseLong(f[0], 16)), body);
		}
		int removed = 0, kept = 0, fixed = 0, ok = 0, failed = 0;
		List<Function> drop = new ArrayList<>();
		for (Function f : fm.getFunctions(true)) {
			AddressSet w = want.get(f.getEntryPoint());
			if (w == null) {
				if (f.getSymbol().getSource() == SourceType.USER_DEFINED) {
					println("keeping unlisted user function " + f.getName() + " @ " + f.getEntryPoint()); kept++;
				} else drop.add(f);
			} else if (!f.getBody().equals(w)) drop.add(f);
			else ok++;
		}
		Map<Address, String> names = new HashMap<>();
		for (Function f : drop) {
			if (want.containsKey(f.getEntryPoint())) names.put(f.getEntryPoint(), f.getName());
			else removed++;
			fm.removeFunction(f.getEntryPoint());
		}
		for (Map.Entry<Address, AddressSet> e : want.entrySet()) {
			Address a = e.getKey();
			if (fm.getFunctionAt(a) != null) continue;
			for (AddressRange r : e.getValue()) {
				Address s = r.getMinAddress();
				if (getInstructionAt(s) == null) {
					currentProgram.getProgramContext().setValue(tmode, s, s, BigInteger.ONE);
					disassemble(s);
				}
			}
			try {
				String n = names.get(a);
				fm.createFunction(n != null && !n.startsWith("FUN_") ? n : null, a, e.getValue(),
					n != null && !n.startsWith("FUN_") ? SourceType.USER_DEFINED : SourceType.DEFAULT);
				fixed++;
			} catch (Exception ex) {
				println("FAILED " + a + ": " + ex.getMessage()); failed++;
			}
		}
		println(String.format("functions: %d matched, %d (re)created, %d removed, %d unlisted user kept, %d failed",
			ok, fixed, removed, kept, failed));
	}
}
